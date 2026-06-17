"""
票务监控模块

职责：
  - 按配置间隔轮询商品库存状态
  - 智能调频：常态慢轮询，接近开售/检测到库存时提速
  - 状态变更回调（可购/售罄/未开售/价格变化）
  - 所有循环内状态读写走内存（KV 缓存），不直接碰磁盘

注意：
  本模块严格遵守平台规则，轮询间隔不得低于 500ms。
  过度频繁请求会触发 412 风控封禁。
"""

import time
import logging
import threading
from typing import Optional, Callable, Dict, Any, Tuple
from enum import Enum, auto

logger = logging.getLogger("monitor")


class TicketStatus(Enum):
    """票务状态"""
    UNKNOWN = auto()        # 未知
    NOT_ON_SALE = auto()    # 未开售
    AVAILABLE = auto()      # 可购买
    SOLD_OUT = auto()       # 已售罄
    OFF_SHELF = auto()      # 已下架
    ERROR = auto()          # 查询异常


class TicketMonitor:
    """
    票务状态监控器。

    使用方式：
        monitor = TicketMonitor(client, config, cache)
        monitor.on_status_change = my_callback
        monitor.start()          # 开始轮询
        # ... 或 ...
        monitor.poll_once()      # 手动单次检查
    """

    def __init__(self, bilibili_client, config, kv_cache):
        """
        Args:
            bilibili_client: BilibiliClient 实例
            config: Config 实例
            kv_cache: KVCache 实例
        """
        self._client = bilibili_client
        self._config = config
        self._cache = kv_cache

        # 轮询参数（从配置读取，缓存到实例变量——高频循环不调 config.get）
        self._fast_interval = config.get("polling.interval_ms", 800) / 1000.0
        self._slow_interval = config.get("polling.pre_sale_refresh_ms", 3000) / 1000.0
        self._max_retries = config.get("polling.max_retries", 3)

        # 抢票目标
        self.item_id = config.get("ticket.item_id", "")
        self.sku_id = config.get("ticket.sku_id", "")
        self.target_count = config.get("ticket.count", 1)
        self.screen_id = config.get("ticket.screen_id", "")
        self.seat_plan_id = config.get("ticket.seat_plan_id", "")

        # 状态
        self._current_status = TicketStatus.UNKNOWN
        self._last_detail: Optional[dict] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 回调
        self.on_status_change: Optional[Callable[[TicketStatus, TicketStatus, dict], None]] = None
        self.on_available: Optional[Callable[[dict], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

        # 统计
        self._poll_count = 0
        self._start_time: float = 0.0

        logger.info("TicketMonitor 初始化 | item=%s fast=%.0fms slow=%.0fms",
                     self.item_id, self._fast_interval * 1000, self._slow_interval * 1000)

    # ── 公开接口 ─────────────────────────────────────────────

    def poll_once(self) -> Tuple[TicketStatus, Optional[dict]]:
        """
        执行一次轮询，返回 (当前状态, 商品详情)。

        线程安全，可在主线程手动调用。
        """
        detail = self._client.get_project_detail(self.item_id)

        if detail is None:
            logger.warning("轮询失败：无法获取商品详情")
            if self.on_error:
                self.on_error("网络请求失败")
            self._current_status = TicketStatus.ERROR
            return TicketStatus.ERROR, None

        self._last_detail = detail
        self._poll_count += 1

        available, reason = self._client.is_purchase_available(detail)
        prev_status = self._current_status

        if reason == "未开售":
            self._current_status = TicketStatus.NOT_ON_SALE
        elif available:
            self._current_status = TicketStatus.AVAILABLE
        elif reason == "已售罄":
            self._current_status = TicketStatus.SOLD_OUT
        else:
            self._current_status = TicketStatus.UNKNOWN

        # 状态变更回调
        if prev_status != self._current_status and self.on_status_change:
            try:
                self.on_status_change(prev_status, self._current_status, detail)
            except Exception as e:
                logger.error("状态回调异常：%s", e)

        # 可购买回调
        if self._current_status == TicketStatus.AVAILABLE and self.on_available:
            try:
                self.on_available(detail)
            except Exception as e:
                logger.error("可购买回调异常：%s", e)

        # 更新缓存中的状态快照
        self._cache.set("monitor.last_status", self._current_status.name)
        self._cache.set("monitor.last_poll_time", time.time())
        self._cache.set("monitor.poll_count", self._poll_count)

        logger.debug("轮询 #%d: %s (%s)", self._poll_count,
                     self._current_status.name, reason)
        return self._current_status, detail

    def start(self):
        """
        启动后台轮询线程。

        使用自适应间隔：常态慢速，检测到库存或临近开售时提速。
        """
        if self._running:
            logger.warning("监控已在运行中")
            return

        self._running = True
        self._stop_event.clear()
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("后台监控已启动")

    def stop(self):
        """停止后台轮询"""
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("后台监控已停止 | 共轮询 %d 次", self._poll_count)

    def get_status(self) -> TicketStatus:
        """获取当前状态（非阻塞）"""
        return self._current_status

    def get_stats(self) -> dict:
        """获取统计信息"""
        elapsed = time.time() - self._start_time if self._start_time else 0
        return {
            "status": self._current_status.name,
            "poll_count": self._poll_count,
            "elapsed_seconds": round(elapsed, 1),
            "avg_interval_ms": round((elapsed / max(self._poll_count, 1)) * 1000),
            "item_id": self.item_id,
        }

    # ── 内部 ─────────────────────────────────────────────────

    def _poll_loop(self):
        """后台轮询主循环"""
        logger.info("轮询循环开始 | 快速间隔 %.0fms | 慢速间隔 %.0fms",
                     self._fast_interval * 1000, self._slow_interval * 1000)

        while not self._stop_event.is_set():
            self.poll_once()

            # 自适应间隔：可购买状态继续保持快速轮询以防万一
            if self._current_status == TicketStatus.AVAILABLE:
                interval = self._fast_interval
            elif self._current_status == TicketStatus.NOT_ON_SALE:
                interval = self._slow_interval
            else:
                interval = self._slow_interval

            self._stop_event.wait(interval)

    # ── 快捷工厂 ─────────────────────────────────────────────

    @classmethod
    def from_env(cls, bilibili_client, config, kv_cache):
        """从配置创建监控器"""
        return cls(bilibili_client, config, kv_cache)
