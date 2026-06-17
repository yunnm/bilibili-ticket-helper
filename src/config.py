"""
配置管理模块

设计思路：
  - 启动时从 YAML 文件一次性加载全部配置到内存
  - 提供点号分隔的路径访问（如 "polling.interval_ms"）
  - 配置值缓存为 Python 原生类型，避免反复解析
  - 支持运行时重新加载（reload）

注意：高频循环中不应调用此模块；抢票开始前应把需要的值提前取出存为局部变量。
"""

import os
import copy
import logging
from typing import Any, Optional

logger = logging.getLogger("config")

try:
    import yaml
except ImportError:
    yaml = None
    logger.warning("PyYAML 未安装，配置加载将受限。请执行：pip install pyyaml")


class Config:
    """配置管理器：YAML 加载 + 内存驻留 + 点路径访问"""

    def __init__(self, config_path: str):
        """
        Args:
            config_path: YAML 配置文件路径
        """
        self._config_path = os.path.abspath(config_path)
        self._data: dict = {}
        self.reload()

    # ── 公开接口 ─────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """
        通过点号路径读取配置值。

        Examples:
            config.get("polling.interval_ms")
            config.get("account.cookie", "")
        """
        keys = key.split(".")
        node = self._data
        for k in keys:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                return default
        return copy.deepcopy(node) if isinstance(node, (dict, list)) else node

    def set(self, key: str, value: Any):
        """运行时设置配置值（仅内存，不写回文件）"""
        keys = key.split(".")
        node = self._data
        for k in keys[:-1]:
            if k not in node or not isinstance(node[k], dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value

    def reload(self):
        """重新加载配置文件"""
        if yaml is None:
            logger.error("无法加载配置：PyYAML 未安装")
            self._data = {}
            return

        if not os.path.exists(self._config_path):
            logger.warning("配置文件不存在：%s，使用空配置", self._config_path)
            self._data = {}
            return

        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
            logger.debug("配置已加载：%s (%d 个顶层键)",
                         self._config_path, len(self._data))
        except (yaml.YAMLError, IOError) as e:
            logger.error("配置文件解析失败：%s", e)
            self._data = {}

    def all(self) -> dict:
        """返回完整配置的深拷贝"""
        return copy.deepcopy(self._data)

    def to_kv_cache(self, kv_cache, prefix: str = "config."):
        """
        将当前配置同步到 KV 缓存中，方便高频循环读取。

        Args:
            kv_cache: KVCache 实例
            prefix: 缓存键前缀
        """
        def _flatten(d, parent_key=""):
            for k, v in d.items():
                full_key = f"{parent_key}{k}"
                if isinstance(v, dict):
                    _flatten(v, full_key + ".")
                else:
                    kv_cache.set(f"{prefix}{full_key}", v)

        _flatten(self._data)

    def validate_required(self, *keys: str) -> list:
        """检查必填配置项，返回缺失的键列表"""
        missing = []
        for key in keys:
            val = self.get(key)
            if val is None or val == "":
                missing.append(key)
        return missing

    def __repr__(self) -> str:
        return f"<Config path={self._config_path} keys={list(self._data.keys())}>"
