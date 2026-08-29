"""feature_manager.py - 特性开关管理器
集中管理所有功能开关，支持热加载（60s mtime 缓存）、运行时切换、变更日志。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Dict, List

FLAGS_FILE = os.path.join(os.path.dirname(__file__), "feature_flags.json")
CHANGELOG_FILE = os.path.join(os.path.dirname(__file__), "feature_flags_changelog.json")
CACHE_TTL = 60  # 秒


class FeatureManager:
    """统一特性开关管理器。单例使用（见 module-level _MANAGER）。"""

    def __init__(self, flags_path: str | None = None) -> None:
        self._flags_path = flags_path or FLAGS_FILE
        self._changelog_path = CHANGELOG_FILE
        self._lock = threading.RLock()
        self._cached_flags: Dict[str, Any] = {}
        self._cached_mtime: float = 0.0
        self._cached_at: float = 0.0
        self._load(force=True)

    # ------------------------------------------------------------------
    # 公共查询 API
    # ------------------------------------------------------------------

    def is_enabled(self, feature_name: str) -> bool:
        """检查某个特性是否启用（带热加载）。"""
        with self._lock:
            self._reload_if_needed()
            feat = self._cached_flags.get(feature_name)
            if feat is None:
                return False
            return bool(feat.get("enabled", False))

    def get_config(self, feature_name: str) -> Dict[str, Any]:
        """获取某个特性的完整配置 dict。"""
        with self._lock:
            self._reload_if_needed()
            feat = self._cached_flags.get(feature_name)
            if feat is None:
                return {}
            return dict(feat.get("config", {}))

    def get_feature(self, feature_name: str) -> Dict[str, Any]:
        """获取单个特性的完整定义（含元数据）。"""
        with self._lock:
            self._reload_if_needed()
            feat = self._cached_flags.get(feature_name)
            if feat is None:
                return {}
            return dict(feat)

    def list_features(self) -> List[Dict[str, Any]]:
        """列出所有特性及其状态。"""
        with self._lock:
            self._reload_if_needed()
            result = []
            for key, feat in self._cached_flags.items():
                if key.startswith("_"):
                    continue
                entry = dict(feat)
                entry["key"] = key
                result.append(entry)
            return result

    def list_by_category(self) -> Dict[str, List[Dict[str, Any]]]:
        """按分类分组列出特性。"""
        cats: Dict[str, List[Dict[str, Any]]] = {}
        for feat in self.list_features():
            cat = feat.get("category", "其他")
            cats.setdefault(cat, []).append(feat)
        return cats

    # ------------------------------------------------------------------
    # 运行时切换
    # ------------------------------------------------------------------

    def toggle_feature(
        self,
        feature_name: str,
        enabled: bool,
        reason: str = "",
        operator: str = "manual",
    ) -> Dict[str, Any]:
        """切换特性开关。返回 {"ok": bool, ...}。"""
        with self._lock:
            self._reload_if_needed()
            feat = self._cached_flags.get(feature_name)
            if feat is None:
                return {"ok": False, "error": f"未知开关: {feature_name}"}

            old_state = bool(feat.get("enabled", False))
            if old_state == bool(enabled):
                return {
                    "ok": True,
                    "feature": feature_name,
                    "enabled": old_state,
                    "changed_at": self._now_iso(),
                    "note": "状态未变化",
                }

            feat["enabled"] = bool(enabled)
            feat["_last_changed"] = self._now_iso()
            feat["_last_operator"] = operator

            self._atomic_write_flags()
            self._append_changelog(feature_name, enabled, reason, operator)

            return {
                "ok": True,
                "feature": feature_name,
                "enabled": bool(enabled),
                "changed_at": feat["_last_changed"],
                "old_state": old_state,
                "dangerous": bool(feat.get("dangerous", False)),
            }

    # ------------------------------------------------------------------
    # 变更日志
    # ------------------------------------------------------------------

    def get_change_log(self, limit: int = 20) -> List[Dict[str, Any]]:
        """获取最近 N 条开关变更记录。"""
        try:
            with open(self._changelog_path, encoding="utf-8") as f:
                logs = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return logs[-limit:]

    def clear_change_log(self) -> None:
        """清空变更日志。"""
        self._safe_write(self._changelog_path, [])

    # ------------------------------------------------------------------
    # 内部：加载 / 热加载 / 原子写
    # ------------------------------------------------------------------

    def _load(self, force: bool = False) -> None:
        """从文件加载 flags。force=True 跳过 mtime 检查。"""
        if not os.path.exists(self._flags_path):
            self._cached_flags = {}
            self._cached_mtime = 0.0
            self._cached_at = 0.0
            return
        try:
            st = os.stat(self._flags_path)
            self._cached_mtime = st.st_mtime
            self._cached_at = time.time()
            with open(self._flags_path, encoding="utf-8") as f:
                self._cached_flags = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._cached_flags = {}
            self._cached_mtime = 0.0
            self._cached_at = 0.0

    def _reload_if_needed(self) -> None:
        """60s 缓存 + mtime 检测：文件变动或缓存过期则重载。"""
        now = time.time()
        if now - self._cached_at < CACHE_TTL and self._cached_mtime > 0:
            try:
                st = os.stat(self._flags_path)
                if st.st_mtime == self._cached_mtime:
                    return
            except OSError:
                return
        self._load(force=True)

    def _atomic_write_flags(self) -> None:
        """原子写回 feature_flags.json。"""
        data = self._cached_flags
        data["_last_updated"] = self._now_iso()
        self._safe_write(self._flags_path, data)

    def _safe_write(self, path: str, obj: Any) -> None:
        """临时文件 + os.replace 原子写盘。"""
        dirname = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=dirname, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _append_changelog(self, feature: str, enabled: bool, reason: str, operator: str) -> None:
        try:
            with open(self._changelog_path, encoding="utf-8") as f:
                logs = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            logs = []
        logs.append(
            {
                "time": self._now_iso(),
                "feature": feature,
                "action": "enabled" if enabled else "disabled",
                "reason": reason,
                "operator": operator,
            }
        )
        self._safe_write(self._changelog_path, logs)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# 模块级单例 — 供 runner 直接 import 使用
_MANAGER: FeatureManager | None = None


def get_manager() -> FeatureManager:
    """获取 FeatureManager 全局单例。"""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = FeatureManager()
    return _MANAGER
