"""
Bilibili API 客户端

封装 B 站会员购相关接口，包括：
  - 登录态管理（Cookie / 扫码登录）
  - 请求头伪造与风控规避
  - 令牌桶限速
  - 会员购业务 API（商品详情、创建订单、确认订单等）

注意：
  本模块内置请求频率控制，严禁移除或绕过限速逻辑。
  过度频繁的请求会触发 412 Precondition Failed，导致 IP 封禁。
"""

import os
import re
import time
import json
import logging
import threading
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlencode

import requests

logger = logging.getLogger("bilibili")

# ── 常量 ──────────────────────────────────────────────────

API_BASE = "https://show.bilibili.com/api/ticket"
MAIN_API_BASE = "https://api.bilibili.com"
PASSPORT_BASE = "https://passport.bilibili.com"

# 各接口端点
ENDPOINTS = {
    # 用户认证
    "user_info": f"{MAIN_API_BASE}/x/web-interface/nav",
    "qrcode_generate": f"{PASSPORT_BASE}/x/passport-login/web/qrcode/generate",
    "qrcode_poll": f"{PASSPORT_BASE}/x/passport-login/web/qrcode/poll",
    # 商品信息
    "project_detail": f"{API_BASE}/project/getV2",
    "project_detail_v1": f"{API_BASE}/project/get",
    # 订单
    "prepare_order": f"{API_BASE}/order/prepare",
    "create_order": f"{API_BASE}/order/createV2",
    "order_info": f"{API_BASE}/order/orderInfo",
    # 地址
    "address_list": f"{API_BASE}/addr/list",
}

# 默认请求头
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://show.bilibili.com",
    "Referer": "https://show.bilibili.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}


# ── 令牌桶限速器 ──────────────────────────────────────────

class TokenBucket:
    """
    令牌桶算法限速器，线程安全。

    以恒定速率生成令牌，每次请求消耗一个令牌。
    支持突发（burst）——桶内可积累少量富余令牌。
    """

    def __init__(self, rate: float, burst: int = 2):
        """
        Args:
            rate: 每秒填充令牌数（max_rps）
            burst: 桶容量（可突发请求数）
        """
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """
        获取一个令牌。返回需要等待的秒数（0 表示立即可用）。
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._last_refill = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            else:
                wait = (1.0 - self._tokens) / self._rate
                self._tokens = 0.0
                return wait

    def update_rate(self, rate: float):
        """运行时调整速率"""
        with self._lock:
            self._rate = rate


# ── B 站客户端 ────────────────────────────────────────────

class BilibiliClient:
    """Bilibili 会员购 API 客户端"""

    def __init__(self, config, kv_cache):
        """
        Args:
            config: Config 实例
            kv_cache: KVCache 实例
        """
        self._config = config
        self._cache = kv_cache
        self._session = requests.Session()

        # 从配置读取限速参数，初始化令牌桶
        max_rps = config.get("rate_limit.max_rps", 1.5)
        burst = config.get("rate_limit.burst", 2)
        self._bucket = TokenBucket(rate=max_rps, burst=burst)
        self._cooldown = config.get("rate_limit.cooldown_seconds", 30)
        self._backoff_base = config.get("polling.backoff_base_ms", 1000) / 1000.0
        self._backoff_mult = config.get("polling.backoff_multiplier", 2.0)

        # CSRF token（从 Cookie 中解析 bili_jct）
        self._csrf = ""

        # 加载或设置 Cookie
        self._init_session()

        logger.info("BilibiliClient 初始化完成 | max_rps=%.1f burst=%d", max_rps, burst)

    # ── 会话初始化 ──────────────────────────────────────────

    def _init_session(self):
        """从配置或缓存加载 Cookie 到 Session"""
        cookie_str = self._config.get("account.cookie", "")
        if not cookie_str:
            cookie_str = self._cache.get("session.cookie", "")

        if cookie_str:
            self._apply_cookie(cookie_str)
            logger.info("已加载 Cookie")

    def _apply_cookie(self, cookie_str: str):
        """解析 Cookie 字符串并应用到 Session"""
        self._session.cookies.clear()
        for item in cookie_str.split(";"):
            item = item.strip()
            if "=" in item:
                key, _, value = item.partition("=")
                self._session.cookies.set(key.strip(), value)

        # 提取 CSRF token
        for cookie in self._session.cookies:
            if cookie.name == "bili_jct":
                self._csrf = cookie.value
                break

    # ── 登录 ────────────────────────────────────────────────

    def check_login(self) -> Tuple[bool, Optional[dict]]:
        """
        检查当前登录状态。

        Returns:
            (is_logged_in, user_info_dict)
        """
        resp = self._request("GET", ENDPOINTS["user_info"])
        if resp is None:
            return False, None
        data = resp.get("data", {})
        is_login = data.get("isLogin", False)
        return is_login, data if is_login else None

    def login_by_qrcode(self) -> bool:
        """
        扫码登录流程。

        Returns:
            True 登录成功
        """
        # 1. 获取二维码
        resp = self._request("GET", ENDPOINTS["qrcode_generate"])
        if resp is None or resp.get("code") != 0:
            logger.error("获取登录二维码失败：%s", resp)
            return False

        qr_data = resp.get("data", {})
        qrcode_key = qr_data.get("qrcode_key", "")
        qr_url = qr_data.get("url", "")

        if not qrcode_key or not qr_url:
            logger.error("二维码数据异常")
            return False

        # 2. 生成终端二维码
        try:
            import qrcode as qrcode_lib
            qr = qrcode_lib.QRCode(border=2)
            qr.add_data(qr_url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            logger.warning("qrcode 库未安装，请在浏览器打开：%s", qr_url)

        print(f"\n请使用 Bilibili 客户端扫描二维码登录")
        print(f"或手动访问：{qr_url}\n")

        # 3. 轮询扫码结果
        for i in range(120):  # 最多等 2 分钟
            resp = self._request("GET", ENDPOINTS["qrcode_poll"], params={
                "qrcode_key": qrcode_key
            })
            if resp is None:
                time.sleep(2)
                continue

            code = resp.get("code")
            data = resp.get("data", {})

            if code == 0:
                # 登录成功，保存 Cookie
                cookie_str = self._extract_cookie_string()
                self._cache.set("session.cookie", cookie_str)
                self._cache.set("session.csrf", self._csrf)
                self._cache.flush()
                logger.info("扫码登录成功")
                print("✓ 登录成功！")
                return True
            elif code == 86038:
                logger.warning("二维码已过期")
                print("✗ 二维码已过期，请重新获取")
                return False
            elif code == 86090:
                # 已扫码但未确认
                if i == 0:
                    print("已扫码，请在手机上确认...")
            elif code == 86101:
                # 等待扫码
                pass

            time.sleep(1)

        logger.warning("登录超时")
        print("✗ 登录超时")
        return False

    def set_cookie(self, cookie_str: str):
        """手动设置 Cookie 字符串"""
        self._apply_cookie(cookie_str)
        self._cache.set("session.cookie", cookie_str)
        self._cache.set("session.csrf", self._csrf)
        self._cache.flush()
        logger.info("Cookie 已更新")

    # ── 会员购 API ──────────────────────────────────────────

    def get_project_detail(self, item_id: str) -> Optional[dict]:
        """
        获取商品详情（V2 接口）。

        Args:
            item_id: 商品 ID
        """
        params = {"version": "134", "id": item_id}
        return self._request("GET", ENDPOINTS["project_detail"], params=params)

    def get_project_detail_v1(self, item_id: str) -> Optional[dict]:
        """获取商品详情（V1 接口）"""
        params = {"version": "134", "id": item_id}
        return self._request("GET", ENDPOINTS["project_detail_v1"], params=params)

    def prepare_order(self, item_id: str, sku_id: str, count: int = 1,
                      screen_id: str = "", seat_plan_id: str = "") -> Optional[dict]:
        """
        预下单（获取订单 token）。

        Returns:
            dict with token, pay_money, etc.
        """
        payload = {
            "project_id": item_id,
            "sku_id": sku_id,
            "count": str(count),
            "screen_id": screen_id,
            "seat_plan_id": seat_plan_id,
            "order_type": "1",
        }
        resp = self._request("POST", ENDPOINTS["prepare_order"], data=payload)
        return resp

    def create_order(self, item_id: str, sku_id: str, count: int = 1,
                     token: str = "", screen_id: str = "",
                     seat_plan_id: str = "", phone: str = "",
                     pay_money: str = "") -> Optional[dict]:
        """
        创建订单（正式下单）。

        Args:
            token: 从 prepare_order 获取的 token
            pay_money: 从 prepare_order 获取的支付金额
        """
        payload = {
            "project_id": item_id,
            "sku_id": sku_id,
            "count": str(count),
            "token": token,
            "screen_id": screen_id,
            "seat_plan_id": seat_plan_id,
            "phone": phone,
            "pay_money": pay_money,
            "order_type": "1",
            "couponCode": "",
            "couponToken": "",
            "deviceId": "",
        }
        resp = self._request("POST", ENDPOINTS["create_order"], data=payload)
        return resp

    def get_order_info(self, order_id: str) -> Optional[dict]:
        """获取订单详情"""
        params = {"orderId": order_id}
        return self._request("GET", ENDPOINTS["order_info"], params=params)

    def get_address_list(self) -> Optional[dict]:
        """获取收货地址列表"""
        return self._request("GET", ENDPOINTS["address_list"])

    # ── 底层请求 ────────────────────────────────────────────

    def _request(
        self,
        method: str,
        url: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        headers: Optional[dict] = None,
        retries: int = 3,
    ) -> Optional[dict]:
        """
        统一请求方法，内置：
          - 令牌桶限速
          - 自动重试 + 指数退避
          - 412 风控检测
          - Cookie 自动回写

        Returns:
            JSON 解析后的 dict，失败返回 None
        """
        # 令牌桶等待
        wait = self._bucket.acquire()
        if wait > 0:
            time.sleep(wait)

        # 构建请求头
        req_headers = DEFAULT_HEADERS.copy()
        if headers:
            req_headers.update(headers)

        # CSRF token（POST 需要）
        if method.upper() == "POST" and self._csrf:
            if data is None:
                data = {}
            if "csrf" not in data:
                data["csrf"] = self._csrf
            if "csrf_token" not in data:
                data["csrf_token"] = self._csrf

        last_error = None
        for attempt in range(retries):
            try:
                if method.upper() == "GET":
                    resp = self._session.get(
                        url, params=params, headers=req_headers, timeout=10
                    )
                elif method.upper() == "POST":
                    resp = self._session.post(
                        url, data=data, headers=req_headers, timeout=10
                    )
                else:
                    logger.error("不支持的请求方法：%s", method)
                    return None

                # 412 风控 —— 冷却后重试
                if resp.status_code == 412:
                    logger.warning("触发 412 风控，冷却 %d 秒...", self._cooldown)
                    time.sleep(self._cooldown)
                    continue

                # 非 200 状态码
                if resp.status_code != 200:
                    logger.warning("HTTP %d: %s", resp.status_code, url[:60])
                    if attempt < retries - 1:
                        delay = self._backoff_base * (self._backoff_mult ** attempt)
                        time.sleep(delay)
                    continue

                # 解析 JSON
                result = resp.json()
                code = result.get("code", -1)

                # 业务错误码
                if code == -401:
                    logger.error("未登录或登录过期，请重新登录")
                    return None
                if code == -101:
                    logger.warning("账号未登录")
                    return None

                return result

            except requests.exceptions.Timeout as e:
                last_error = e
                logger.warning("请求超时 (attempt %d/%d): %s", attempt + 1, retries, url[:60])
                if attempt < retries - 1:
                    time.sleep(self._backoff_base * (self._backoff_mult ** attempt))

            except requests.exceptions.ConnectionError as e:
                last_error = e
                logger.warning("连接错误 (attempt %d/%d): %s", attempt + 1, retries, e)
                if attempt < retries - 1:
                    time.sleep(2)

            except (json.JSONDecodeError, ValueError) as e:
                last_error = e
                logger.error("JSON 解析失败：%s", e)
                return None

            except Exception as e:
                last_error = e
                logger.error("请求异常: %s", e)
                if attempt < retries - 1:
                    time.sleep(self._backoff_base)

        logger.error("请求最终失败 (after %d retries): %s", retries, last_error)
        return None

    def _extract_cookie_string(self) -> str:
        """从 Session 提取 Cookie 字符串"""
        parts = []
        for cookie in self._session.cookies:
            parts.append(f"{cookie.name}={cookie.value}")
        return "; ".join(parts)

    # ── 便捷方法 ────────────────────────────────────────────

    def is_purchase_available(self, project_detail: dict) -> Tuple[bool, str]:
        """
        判断商品是否可购买。

        Returns:
            (is_available, reason)
        """
        data = project_detail.get("data", {})
        if not data:
            return False, "无数据"

        # 多种可能的可购买状态字段
        is_sale = data.get("is_sale", 0)  # 1 = 可售
        sale_flag = str(data.get("sale_flag", ""))
        express = data.get("express", "")

        # express 字段常见值含义：
        # ""  或 "1" = 可购买
        # "2" = 已售罄
        # "3" = 未开售
        if express == "2":
            return False, "已售罄"
        if express == "3":
            return False, "未开售"

        if is_sale == 1 or sale_flag == "1":
            return True, "可购买"

        return False, f"状态: is_sale={is_sale}, sale_flag={sale_flag}, express={express}"


    def generate_qrcode(self) -> Optional[dict]:
        resp = self._request("GET", ENDPOINTS.get("qrcode_generate", ""))
        if resp is None or resp.get("code") != 0:
            return None
        data = resp.get("data", {})
        return {"qrcode_key": data.get("qrcode_key", ""), "url": data.get("url", "")}

    def poll_qrcode(self, qrcode_key: str) -> Optional[dict]:
        return self._request("GET", ENDPOINTS.get("qrcode_poll", ""),
                             params={"qrcode_key": qrcode_key})

    def extract_skus(self, project_detail: dict) -> list:
        """从商品详情提取 SKU 列表"""
        data = project_detail.get("data", {})
        sku_list = data.get("sku_list", []) or data.get("skus", []) or []
        if not sku_list:
            # 某些接口把 SKU 放在 screen_list 里
            screens = data.get("screen_list", [])
            for screen in screens:
                tickets = screen.get("ticket_list", [])
                sku_list.extend(tickets)
        return sku_list
