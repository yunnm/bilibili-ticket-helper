"""
本地 KV 缓存模块

设计思路：
  - 内存 dict 作为一级存储，所有读写零磁盘 IO
  - 写入时标记脏位，由定时刷盘或退出时统一写回
  - 支持 TTL 过期，自动清理
  - JSON 文件作为持久化后端，人类可读、便于调试

注意：本模块并非通用分布式缓存，仅为抢票高频循环场景设计，
      强调「读极快、写不卡 IO、退出不丢数据」。
"""

import os
import json
import time
import atexit
import threading
import logging
from typing import Any, Optional, Dict, Tuple

logger = logging.getLogger("kv_cache")


class KVCache:
    """本地 KV 缓存：内存读写 + JSON 文件持久化"""

    def __init__(
        self,
        cache_dir: str = "./data/cache",
        file_name: str = "kv_store.json",
        flush_interval: float = 5.0,
        auto_flush: bool = True,
    ):
        """
        Args:
            cache_dir: 缓存文件目录
            file_name: 持久化文件名
            flush_interval: 自动刷盘间隔（秒），0 表示每次 set 都刷
            auto_flush: 是否注册 atexit 自动刷盘
        """
        self._cache_dir = os.path.abspath(cache_dir)
        self._file_path = os.path.join(self._cache_dir, file_name)
        self._flush_interval = flush_interval
        self._lock = threading.Lock()

        # 内存存储: key -> (value, expiry_timestamp | None)
        self._store: Dict[str, Tuple[Any, Optional[float]]] = {}
        self._dirty = False
        self._last_flush = time.time()
        self._running = True

        os.makedirs(self._cache_dir, exist_ok=True)
        self._load()

        if auto_flush:
            atexit.register(self._on_exit)

        logger.debug("KVCache 初始化完成，%d 条记录已加载", len(self._store))

    # ── 公开接口 ─────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """从内存读取，无磁盘 IO"""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return default
            value, expiry = entry
            if expiry is not None and time.time() > expiry:
                del self._store[key]
                self._dirty = True
                return default
            return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None):
        """写入内存，标记脏位。ttl 单位：秒"""
        with self._lock:
            expiry = None if ttl is None else time.time() + ttl
            self._store[key] = (value, expiry)
            self._dirty = True

        self._maybe_flush()

    def delete(self, key: str):
        """删除键"""
        with self._lock:
            if key in self._store:
                del self._store[key]
                self._dirty = True
        self._maybe_flush()

    def has(self, key: str) -> bool:
        """检查键是否存在且未过期"""
        return self.get(key, _SENTINEL) is not _SENTINEL

    def keys(self) -> list:
        """返回所有有效键（已过滤过期）"""
        now = time.time()
        with self._lock:
            return [
                k for k, (_, exp) in self._store.items()
                if exp is None or exp > now
            ]

    def clear(self):
        """清空缓存"""
        with self._lock:
            self._store.clear()
            self._dirty = True
        self._force_flush()

    def flush(self):
        """手动刷盘"""
        self._force_flush()

    # ── 内部方法 ─────────────────────────────────────────────

    def _load(self):
        """从磁盘加载缓存"""
        if not os.path.exists(self._file_path):
            return
        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            now = time.time()
            loaded = 0
            for key, entry in data.items():
                if isinstance(entry, list) and len(entry) == 2:
                    value, expiry = entry
                    if expiry is not None and now > expiry:
                        continue  # 已过期，跳过
                    self._store[key] = (value, expiry)
                    loaded += 1
            logger.debug("从磁盘加载 %d 条缓存记录（跳过 %d 条过期）",
                         loaded, len(data) - loaded)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("缓存文件损坏，将重新创建：%s", e)
            self._store = {}

    def _force_flush(self):
        """无条件刷盘"""
        with self._lock:
            self._do_flush()

    def _do_flush(self):
        """执行实际写入（需在锁内调用）"""
        now = time.time()
        data = {}
        for key, (value, expiry) in self._store.items():
            data[key] = [value, expiry]
        try:
            tmp_path = self._file_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, default=str)
            os.replace(tmp_path, self._file_path)
            self._dirty = False
            self._last_flush = time.time()
        except IOError as e:
            logger.error("缓存刷盘失败：%s", e)

    def _maybe_flush(self):
        """根据间隔决定是否刷盘"""
        if self._flush_interval == 0:
            self._force_flush()
        elif self._dirty and (time.time() - self._last_flush) >= self._flush_interval:
            self._force_flush()

    def _on_exit(self):
        """程序退出时确保刷盘"""
        if self._dirty:
            self._force_flush()
            logger.debug("退出时已刷盘")

    def __len__(self) -> int:
        return len(self.keys())

    def __contains__(self, key: str) -> bool:
        return self.has(key)

    def __repr__(self) -> str:
        return f"<KVCache keys={len(self)} dirty={self._dirty}>"


# 哨兵对象，用于区分「不存在」和「值为 None」
_SENTINEL = object()
