"""
参数版本管理模块 (Phase 7, Task 4)

功能：
- 保存每个版本的参数配置（JSON 格式）
- 支持版本查询、回滚
- 记录版本变更说明（changelog）
- 与 four_dim_strategy 的 per_symbol_risk 配置格式兼容

目录结构：
    monitor/versions/
        v001_20260830_phase6_baseline.json   # 初始基线版本（Phase 6 结果）
        v002_20261231_q4_reopt.json         # 季度重优化版本
        ...
        versions.json                        # 版本索引（元数据）

用法：
    from monitor.param_versions import ParamVersionManager
    mgr = ParamVersionManager()
    version_id = mgr.save_version(params_dict, "季度重优化 Q4", author="auto")
    params = mgr.load_version(version_id)
    versions = mgr.list_versions()
    mgr.rollback_to(version_id)  # 回滚并返回参数
"""

import copy
import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

VERSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "versions")
VERSIONS_INDEX = os.path.join(VERSIONS_DIR, "versions.json")

# 必须存在的参数字段（per_symbol_risk 格式）
REQUIRED_FIELDS = ["stop_atr_mult", "rr_ratio"]
OPTIONAL_FIELDS = ["T_thresh", "note"]


class ParamVersionManager:
    """参数版本管理器"""

    def __init__(self, versions_dir: Optional[str] = None):
        self.versions_dir = versions_dir or VERSIONS_DIR
        self._ensure_dirs()
        self._index = self._load_index()

    def _ensure_dirs(self):
        os.makedirs(self.versions_dir, exist_ok=True)

    def _load_index(self) -> Dict[str, Any]:
        if os.path.exists(VERSIONS_INDEX):
            with open(VERSIONS_INDEX, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"versions": [], "current_version": None}

    def _save_index(self):
        with open(VERSIONS_INDEX, "w", encoding="utf-8") as f:
            json.dump(self._index, f, indent=2, ensure_ascii=False)

    def _next_version_id(self) -> str:
        """生成下一个版本号 v001, v002, ..."""
        existing = len(self._index["versions"])
        return f"v{existing + 1:03d}"

    def save_version(
        self,
        params: Dict[str, Dict[str, float]],
        description: str,
        author: str = "auto",
        validation_summary: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        保存一个新版本的参数配置。

        Args:
            params: 品种参数字典，格式 {symbol: {stop_atr_mult, rr_ratio, T_thresh?, note?}}
            description: 版本说明
            author: 作者/来源标识
            validation_summary: 验证结果摘要（可选）

        Returns:
            version_id: 新版本号
        """
        version_id = self._next_version_id()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        date_tag = datetime.now().strftime("%Y%m%d")

        # 验证参数格式
        self._validate_params(params)

        # 保存参数文件
        filename = f"{version_id}_{date_tag}.json"
        filepath = os.path.join(self.versions_dir, filename)

        version_data = {
            "version_id": version_id,
            "timestamp": timestamp,
            "description": description,
            "author": author,
            "n_symbols": len(params),
            "validation_summary": validation_summary or {},
            "params": params,
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(version_data, f, indent=2, ensure_ascii=False)

        # 更新索引
        self._index["versions"].append({
            "version_id": version_id,
            "timestamp": timestamp,
            "description": description,
            "author": author,
            "filename": filename,
            "n_symbols": len(params),
            "validation_summary": validation_summary or {},
        })

        # 设为当前版本
        self._index["current_version"] = version_id
        self._save_index()

        return version_id

    def load_version(self, version_id: str) -> Dict[str, Dict[str, float]]:
        """加载指定版本的参数配置"""
        entry = self._find_version_entry(version_id)
        if not entry:
            raise ValueError(f"版本 {version_id} 不存在")

        filepath = os.path.join(self.versions_dir, entry["filename"])
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        return data["params"]

    def load_current(self) -> Dict[str, Dict[str, float]]:
        """加载当前（最新）版本的参数"""
        if not self._index["current_version"]:
            raise ValueError("没有可用的参数版本")
        return self.load_version(self._index["current_version"])

    def get_version_info(self, version_id: str) -> Dict[str, Any]:
        """获取版本的元数据信息"""
        entry = self._find_version_entry(version_id)
        if not entry:
            raise ValueError(f"版本 {version_id} 不存在")
        return copy.deepcopy(entry)

    def list_versions(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """列出所有版本（按时间倒序）"""
        versions = list(reversed(self._index["versions"]))
        if limit:
            versions = versions[:limit]
        return versions

    def compare_versions(
        self, version_a: str, version_b: str
    ) -> Dict[str, Dict[str, Dict[str, float]]]:
        """
        比较两个版本的参数差异。

        Returns:
            {symbol: {param_name: {from: val_a, to: val_b, delta: delta}}}
        """
        params_a = self.load_version(version_a)
        params_b = self.load_version(version_b)

        all_symbols = set(params_a.keys()) | set(params_b.keys())
        diffs = {}

        for sym in sorted(all_symbols):
            a = params_a.get(sym, {})
            b = params_b.get(sym, {})
            all_params = set(a.keys()) | set(b.keys())

            sym_diff = {}
            for p in all_params:
                if p == "note":
                    continue
                va = a.get(p)
                vb = b.get(p)
                if va != vb:
                    delta = (vb - va) if (va is not None and vb is not None) else None
                    sym_diff[p] = {"from": va, "to": vb, "delta": delta}

            if sym_diff:
                diffs[sym] = sym_diff

        return diffs

    def rollback_to(self, version_id: str) -> Dict[str, Dict[str, float]]:
        """
        回滚到指定版本（将该版本设为 current，返回参数）。
        注意：这只是版本标记的回滚，实际策略代码中的参数需要另行同步。
        """
        if not self._find_version_entry(version_id):
            raise ValueError(f"版本 {version_id} 不存在")

        self._index["current_version"] = version_id
        self._save_index()
        return self.load_version(version_id)

    def export_per_symbol_risk(
        self, version_id: Optional[str] = None
    ) -> Dict[str, Dict[str, float]]:
        """
        导出为 four_dim_strategy 兼容的 per_symbol_risk 格式。
        只包含 stop_atr_mult 和 rr_ratio。
        """
        params = self.load_version(version_id) if version_id else self.load_current()
        result = {}
        for sym, p in params.items():
            entry = {
                "stop_atr_mult": p["stop_atr_mult"],
                "rr_ratio": p["rr_ratio"],
            }
            if "note" in p:
                entry["note"] = p["note"]
            result[sym] = entry
        return result

    def _find_version_entry(self, version_id: str) -> Optional[Dict[str, Any]]:
        for v in self._index["versions"]:
            if v["version_id"] == version_id:
                return v
        return None

    def _validate_params(self, params: Dict[str, Dict[str, float]]):
        """验证参数格式的合法性"""
        if not isinstance(params, dict):
            raise ValueError("参数必须是字典格式")

        for sym, p in params.items():
            if not isinstance(p, dict):
                raise ValueError(f"品种 {sym} 的参数必须是字典")
            for field in REQUIRED_FIELDS:
                if field not in p:
                    raise ValueError(f"品种 {sym} 缺少必填字段 {field}")
                if not isinstance(p[field], (int, float)):
                    raise ValueError(f"品种 {sym} 的 {field} 必须是数字")

            # 合理性检查
            if p["stop_atr_mult"] <= 0:
                raise ValueError(f"品种 {sym} 的 stop_atr_mult 必须 > 0")
            if p["rr_ratio"] < 1.0:
                raise ValueError(f"品种 {sym} 的 rr_ratio 必须 >= 1.0")
            if "T_thresh" in p and p["T_thresh"] <= 0:
                raise ValueError(f"品种 {sym} 的 T_thresh 必须 > 0")


def init_baseline_from_phase6(
    phase6_params_path: str,
    phase6_validation_path: Optional[str] = None,
) -> str:
    """
    从 Phase 6 的最终参数初始化基线版本 (v001)。
    如果版本目录已有内容，则跳过。

    Args:
        phase6_params_path: phase6_final_params.json 的路径
        phase6_validation_path: phase6_final_validation.json 的路径（可选）

    Returns:
        version_id: 创建的版本号（或已存在的版本号）
    """
    mgr = ParamVersionManager()

    # 检查是否已经初始化过
    if mgr.list_versions():
        return mgr._index["versions"][0]["version_id"]

    # 加载 Phase 6 参数
    with open(phase6_params_path, "r", encoding="utf-8") as f:
        phase6_params = json.load(f)

    # 转换为 per_symbol_risk 兼容格式
    params = {}
    for sym, p in phase6_params.items():
        params[sym] = {
            "stop_atr_mult": p.get("stop_atr_mult", 1.5),
            "rr_ratio": p.get("rr_ratio", 2.0),
            "T_thresh": p.get("T_thresh"),
            "note": f"P6基线: expR+{p.get('delta', 0):.3f}",
        }

    # 加载验证摘要
    validation_summary = {}
    if phase6_validation_path and os.path.exists(phase6_validation_path):
        with open(phase6_validation_path, "r", encoding="utf-8") as f:
            vdata = json.load(f)
        passing = [r for r in vdata.get("results", []) if r.get("passes_validation")]
        all_results = vdata.get("results", [])
        if passing:
            avg_delta = sum(r["oos"]["delta"] for r in passing) / len(passing)
            validation_summary = {
                "method": vdata.get("method", "unknown"),
                "n_total": len(all_results),
                "n_passing": len(passing),
                "avg_oos_delta": round(avg_delta, 4),
                "params": vdata.get("params", {}),
            }

    version_id = mgr.save_version(
        params=params,
        description="Phase 6 紧边界优化基线版本（13个品种上线）",
        author="phase6_import",
        validation_summary=validation_summary,
    )

    return version_id


if __name__ == "__main__":
    # 命令行工具：初始化基线版本
    import sys

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    phase6_params = os.path.join(project_root, "ga_results", "phase6_final_params.json")
    phase6_validation = os.path.join(project_root, "ga_results", "phase6_final_validation.json")

    if not os.path.exists(phase6_params):
        print(f"错误: 找不到 {phase6_params}")
        sys.exit(1)

    vid = init_baseline_from_phase6(phase6_params, phase6_validation)
    mgr = ParamVersionManager()
    info = mgr.get_version_info(vid)
    print(f"✓ 基线版本初始化完成: {vid}")
    print(f"  时间: {info['timestamp']}")
    print(f"  品种数: {info['n_symbols']}")
    print(f"  说明: {info['description']}")

    print("\n所有版本:")
    for v in mgr.list_versions():
        print(f"  {v['version_id']}  {v['timestamp']}  {v['description']}  ({v['n_symbols']} 品种)")
