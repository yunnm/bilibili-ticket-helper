"""
下单抢票模块

职责：
  - 预下单（prepare）→ 正式下单（create）两步流程
  - 可被监控模块回调自动触发，也支持手动触发
  - 下单成功后将订单信息写入缓存

注意：
  下单是高频竞争的环节，preflight 阶段要尽量轻量。
  所有关键参数在抢票开始前就缓存到内存，不在循环中读配置/磁盘。
"""

import time
import logging
import threading
from enum import Enum, auto
from typing import Optional, Callable, Dict, Any, List

logger = logging.getLogger("order")


class OrderStatus(Enum):
    """订单状态"""
    IDLE = auto()           # 空闲
    PREPARING = auto()      # 预下单中
    CREATING = auto()       # 正式下单中
    SUCCESS = auto()        # 下单成功
    FAILED = auto()         # 下单失败
    SOLD_OUT = auto()       # 售罄


class OrderResult:
    """下单结果"""
    def __init__(self, status: OrderStatus, order_id: str = "",
                 message: str = "", raw_data: Optional[dict] = None):
        self.status = status
        self.order_id = order_id
        self.message = message
        self.raw_data = raw_data or {}

    @property
    def is_success(self) -> bool:
        return self.status == OrderStatus.SUCCESS

    def __repr__(self) -> str:
        return f"<OrderResult {self.status.name} order={self.order_id} msg={self.message}>"


class OrderExecutor:
    """
    订单执行器。

    使用方式：
        executor = OrderExecutor(client, config, cache)

        # 手动下单
        result = executor.execute()

        # 作为监控回调，自动抢票
        monitor.on_available = executor.on_ticket_available
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

        # 关键参数 —— 初始化时一次性读到内存，后续零 IO
        self._item_id = config.get("ticket.item_id", "")
        self._sku_id = config.get("ticket.sku_id", "")
        self._count = config.get("ticket.count", 1)
        self._screen_id = config.get("ticket.screen_id", "")
        self._seat_plan_id = config.get("ticket.seat_plan_id", "")
        self._is_select_seat = config.get("ticket.is_select_seat", False)
        self._phone = config.get("ticket.phone", "")

        self._lock = threading.Lock()
        self._last_result: Optional[OrderResult] = None
        self._attempt_count = 0

        # 回调
        self.on_success: Optional[Callable[[OrderResult], None]] = None
        self.on_failure: Optional[Callable[[OrderResult], None]] = None

        logger.info("OrderExecutor 初始化 | item=%s sku=%s count=%d",
                     self._item_id, self._sku_id, self._count)

    # ── 公开接口 ─────────────────────────────────────────────

    def execute(self) -> OrderResult:
        """
        执行完整下单流程：prepare → create。

        线程安全（加锁），防止重复下单。

        Returns:
            OrderResult
        """
        with self._lock:
            self._attempt_count += 1
            attempt = self._attempt_count
            logger.info("===== 下单尝试 #%d =====", attempt)

            # Step 1: 预下单
            self._last_result = OrderResult(OrderStatus.PREPARING)
            prepare_resp = self._client.prepare_order(
                item_id=self._item_id,
                sku_id=self._sku_id,
                count=self._count,
                screen_id=self._screen_id,
                seat_plan_id=self._seat_plan_id,
            )

            if prepare_resp is None:
                return self._fail("预下单网络请求失败")
            if prepare_resp.get("code") != 0:
                err_msg = prepare_resp.get("message", prepare_resp.get("msg", "预下单失败"))
                logger.warning("预下单失败: %s (code=%s)", err_msg, prepare_resp.get("code"))
                if "售罄" in str(err_msg) or "无票" in str(err_msg):
                    return self._fail_sold_out(err_msg)
                return self._fail(err_msg)

            prepare_data = prepare_resp.get("data", {})
            token = prepare_data.get("token", "")
            pay_money = str(prepare_data.get("pay_money", ""))
            logger.info("预下单成功 | token=%s... pay=%.2f",
                         token[:20] if token else "N/A",
                         float(pay_money) if pay_money else 0)

            # Step 2: 正式下单
            self._last_result = OrderResult(OrderStatus.CREATING)
            create_resp = self._client.create_order(
                item_id=self._item_id,
                sku_id=self._sku_id,
                count=self._count,
                token=token,
                screen_id=self._screen_id,
                seat_plan_id=self._seat_plan_id,
                phone=self._phone,
                pay_money=pay_money,
            )

            if create_resp is None:
                return self._fail("正式下单网络请求失败")
            if create_resp.get("code") != 0:
                err_msg = create_resp.get("message", create_resp.get("msg", "下单失败"))
                logger.warning("正式下单失败: %s (code=%s)", err_msg, create_resp.get("code"))
                if "售罄" in str(err_msg) or "无票" in str(err_msg):
                    return self._fail_sold_out(err_msg)
                return self._fail(err_msg)

            # 成功
            order_data = create_resp.get("data", {})
            order_id = str(order_data.get("orderId", order_data.get("order_id", "")))

            result = OrderResult(
                status=OrderStatus.SUCCESS,
                order_id=order_id,
                message="下单成功",
                raw_data=order_data,
            )
            self._last_result = result

            # 写入缓存
            self._cache.set("order.last_order_id", order_id)
            self._cache.set("order.last_success_time", time.time())
            self._cache.set("order.raw_data", order_data)
            self._cache.flush()

            logger.info("✓✓✓ 下单成功！订单号: %s ✓✓✓", order_id)

            if self.on_success:
                try:
                    self.on_success(result)
                except Exception as e:
                    logger.error("成功回调异常: %s", e)

            return result

    def on_ticket_available(self, project_detail: dict):
        """
        作为监控回调使用：检测到有票时自动下单。

        可直接绑到 TicketMonitor.on_available。
        """
        logger.info("!!! 检测到可购买，立即触发下单 !!!")
        result = self.execute()
        if result.is_success:
            print(f"\n{'='*60}")
            print(f"  🎉 抢票成功！订单号: {result.order_id}")
            print(f"  请尽快前往 Bilibili 会员购完成支付")
            print(f"{'='*60}\n")
        else:
            print(f"\n  抢票失败: {result.message}")

    def get_last_result(self) -> Optional[OrderResult]:
        """获取最近一次下单结果"""
        return self._last_result

    def get_attempt_count(self) -> int:
        """获取尝试次数"""
        return self._attempt_count

    # ── 内部 ─────────────────────────────────────────────────

    def _fail(self, message: str) -> OrderResult:
        result = OrderResult(OrderStatus.FAILED, message=message)
        self._last_result = result
        if self.on_failure:
            try:
                self.on_failure(result)
            except Exception as e:
                logger.error("失败回调异常: %s", e)
        return result

    def _fail_sold_out(self, message: str) -> OrderResult:
        result = OrderResult(OrderStatus.SOLD_OUT, message=message)
        self._last_result = result
        if self.on_failure:
            try:
                self.on_failure(result)
            except Exception as e:
                logger.error("失败回调异常: %s", e)
        return result
