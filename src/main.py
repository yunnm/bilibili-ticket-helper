"""
Bilibili 会员购抢票助手 - 主入口

注意：本工具仅供个人学习与研究使用，不得用于任何形式的商业牟利或黄牛倒卖行为。
使用本工具时请遵守B站平台的相关规则，合理设置请求频率。

使用方式：
    python -m src.main              # 交互式菜单
    pip install -r requirements.txt # 首次运行前安装依赖
"""

import sys
import os
import time
import logging

# 确保项目根在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.kv_cache import KVCache
from src.bilibili_api import BilibiliClient
from src.monitor import TicketMonitor, TicketStatus
from src.order import OrderExecutor, OrderResult


# ── 日志配置 ────────────────────────────────────────────────

def setup_logging(config: Config):
    """配置日志输出"""
    log_dir = config.get("storage.log_dir", "./data/logs")
    os.makedirs(log_dir, exist_ok=True)

    log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(
                os.path.join(log_dir, "ticket.log"),
                encoding="utf-8",
            ),
            # 控制台只显示 WARNING 及以上
            logging.StreamHandler(),
        ],
    )
    # 降低控制台 handler 的级别
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(logging.WARNING)

    logging.getLogger("monitor").setLevel(logging.DEBUG)
    logging.getLogger("order").setLevel(logging.DEBUG)


# ── 全局组件 ────────────────────────────────────────────────

_components = {}


def get_client() -> BilibiliClient:
    return _components["client"]


def get_monitor() -> TicketMonitor:
    return _components["monitor"]


def get_executor() -> OrderExecutor:
    return _components["executor"]


# ── 菜单动作 ────────────────────────────────────────────────

def menu_login_qrcode():
    """扫码登录"""
    client = get_client()
    print("\n正在获取登录二维码...")
    success = client.login_by_qrcode()
    if success:
        # 重新加载 session 状态
        is_login, user_info = client.check_login()
        if is_login and user_info:
            uname = user_info.get("uname", "未知")
            print(f"当前用户: {uname}")


def menu_login_cookie():
    """手动设置 Cookie"""
    client = get_client()
    print("\n请粘贴 B站完整的 Cookie 字符串（从浏览器 F12 → Application → Cookies 复制）：")
    cookie_str = input("Cookie: ").strip()
    if cookie_str:
        client.set_cookie(cookie_str)
        is_login, user_info = client.check_login()
        if is_login and user_info:
            uname = user_info.get("uname", "未知")
            print(f"✓ Cookie 有效，当前用户: {uname}")
        else:
            print("✗ Cookie 无效或已过期，请重新设置")
    else:
        print("未输入 Cookie")


def menu_check_login():
    """检查登录状态"""
    client = get_client()
    print("\n正在检查登录状态...")
    is_login, user_info = client.check_login()
    if is_login and user_info:
        uname = user_info.get("uname", "未知")
        mid = user_info.get("mid", "")
        level = user_info.get("level_info", {}).get("current_level", "")
        print(f"✓ 已登录")
        print(f"  用户名: {uname}")
        print(f"  UID: {mid}")
        print(f"  等级: Lv{level}")
    else:
        print("✗ 未登录，请先选择「扫码登录」或「手动设置 Cookie」")


def menu_set_item():
    """设置抢票目标"""
    config = _components["config"]
    print("\n设置抢票目标")
    item_id = input(f"商品 ID (当前: {config.get('ticket.item_id', '未设置')}): ").strip()
    if item_id:
        config.set("ticket.item_id", item_id)

        # 尝试获取详情并列出 SKU
        client = get_client()
        print("正在获取商品信息...")
        detail = client.get_project_detail(item_id)
        if detail and detail.get("code") == 0:
            data = detail.get("data", {})
            name = data.get("name", data.get("project_name", "未知"))
            print(f"  商品名称: {name}")

            skus = client.extract_skus(detail)
            if skus:
                print(f"\n  可选 SKU ({len(skus)} 个):")
                for i, sku in enumerate(skus):
                    sku_id = sku.get("sku_id", sku.get("id", ""))
                    sku_name = sku.get("name", sku.get("sku_name", ""))
                    price = sku.get("price", sku.get("real_price", ""))
                    stock = sku.get("sale_count", sku.get("stock", "?"))
                    print(f"    [{i+1}] {sku_name}  ¥{price}/100.0  库存: {stock}  ID: {sku_id}")

                choice = input(f"\n选择 SKU (1-{len(skus)}，回车跳过): ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(skus):
                    selected = skus[int(choice) - 1]
                    sku_id = selected.get("sku_id", selected.get("id", ""))
                    config.set("ticket.sku_id", str(sku_id))
                    print(f"已选择: {selected.get('name', sku_id)}")

                    # 如果有场次选项
                    screens = data.get("screen_list", [])
                    if screens:
                        print(f"\n  可选场次 ({len(screens)} 个):")
                        for i, screen in enumerate(screens):
                            sname = screen.get("name", screen.get("screen_name", ""))
                            sid = screen.get("screen_id", screen.get("id", ""))
                            print(f"    [{i+1}] {sname}  ID: {sid}")
                        schoice = input(f"\n选择场次 (1-{len(screens)}，回车跳过): ").strip()
                        if schoice.isdigit() and 1 <= int(schoice) <= len(screens):
                            config.set("ticket.screen_id",
                                       str(screens[int(schoice) - 1].get("screen_id", "")))
        else:
            print("  未能获取商品信息，请检查 ID 是否正确")

    count = input(f"购买数量 (当前: {config.get('ticket.count', 1)}): ").strip()
    if count.isdigit():
        config.set("ticket.count", int(count))

    # 同步到 KV 缓存和 executor
    _sync_components()

    print("\n当前抢票配置：")
    print(f"  商品 ID: {config.get('ticket.item_id')}")
    print(f"  SKU ID:  {config.get('ticket.sku_id')}")
    print(f"  场次 ID: {config.get('ticket.screen_id')}")
    print(f"  数量:    {config.get('ticket.count')}")


def menu_start_monitor():
    """启动后台监控"""
    monitor = get_monitor()
    executor = get_executor()

    if not monitor.item_id or not monitor.sku_id:
        print("\n请先设置抢票目标（菜单选项 4）")
        return

    # 绑定回调：检测到有票自动下单
    monitor.on_available = executor.on_ticket_available

    def on_change(old, new, detail):
        print(f"\n[状态变更] {old.name} → {new.name}")
        if new == TicketStatus.AVAILABLE:
            print("!!! 有票了！正在自动下单...")
        elif new == TicketStatus.SOLD_OUT:
            print("  票已售罄")

    monitor.on_status_change = on_change

    print(f"\n启动后台监控...")
    print(f"  商品: {monitor.item_id}")
    print(f"  快速间隔: {monitor._fast_interval*1000:.0f}ms")
    print(f"  慢速间隔: {monitor._slow_interval*1000:.0f}ms")
    print(f"  检测到有票时将自动下单！")
    print(f"\n按 Ctrl+C 停止监控\n")
    print("-" * 50)

    monitor.start()

    try:
        last_count = 0
        while True:
            time.sleep(2)
            stats = monitor.get_stats()
            if stats["poll_count"] != last_count:
                last_count = stats["poll_count"]
                status_icon = {
                    "AVAILABLE": "🟢",
                    "NOT_ON_SALE": "⏳",
                    "SOLD_OUT": "🔴",
                    "UNKNOWN": "❓",
                    "ERROR": "⚠️",
                }.get(stats["status"], "❓")
                print(f"\r[{status_icon}] 轮询 #{stats['poll_count']} | "
                      f"状态: {stats['status']} | "
                      f"已运行 {stats['elapsed_seconds']:.0f}s", end="", flush=True)
    except KeyboardInterrupt:
        print("\n\n正在停止监控...")
        monitor.stop()
        print("监控已停止")


def menu_manual_order():
    """手动下单"""
    executor = get_executor()
    config = _components["config"]

    if not config.get("ticket.item_id"):
        print("\n请先设置抢票目标（菜单选项 4）")
        return

    confirm = input("\n确认手动下单？这将立即尝试购买 (y/n): ").strip().lower()
    if confirm == "y":
        print("正在下单...")
        result = executor.execute()
        if result.is_success:
            print(f"\n🎉 抢票成功！订单号: {result.order_id}")
            print("请尽快前往 Bilibili 会员购完成支付")
        else:
            print(f"\n下单失败: {result.message}")
    else:
        print("已取消")


def menu_view_config():
    """查看当前配置"""
    config = _components["config"]
    all_cfg = config.all()
    import json
    print("\n当前配置：")
    print(json.dumps(all_cfg, ensure_ascii=False, indent=2, default=str))


def menu_stats():
    """查看统计"""
    monitor = get_monitor()
    executor = get_executor()
    stats = monitor.get_stats()
    print(f"\n运行统计：")
    print(f"  轮询次数: {stats['poll_count']}")
    print(f"  当前状态: {stats['status']}")
    print(f"  下单尝试: {executor.get_attempt_count()}")
    last = executor.get_last_result()
    if last:
        print(f"  最近下单: {last.status.name} {last.message}")


# ── 内部辅助 ────────────────────────────────────────────────

def _sync_components():
    """配置变更后同步各组件参数"""
    config = _components["config"]
    cache = _components["cache"]

    # 重新创建 monitor 和 executor 以应用新参数
    client = _components["client"]

    monitor = TicketMonitor(client, config, cache)
    executor = OrderExecutor(client, config, cache)

    _components["monitor"] = monitor
    _components["executor"] = executor


# ── 主菜单 ──────────────────────────────────────────────────

MENU = [
    ("1", "检查登录状态",    menu_check_login),
    ("2", "扫码登录",        menu_login_qrcode),
    ("3", "手动设置 Cookie",  menu_login_cookie),
    ("4", "设置抢票目标",    menu_set_item),
    ("5", "启动监控抢票",    menu_start_monitor),
    ("6", "手动下单",        menu_manual_order),
    ("7", "查看当前配置",    menu_view_config),
    ("8", "运行统计",        menu_stats),
    ("0", "退出",            None),
]


def print_banner():
    print()
    print("╔" + "═" * 58 + "╗")
    print("║" + "   Bilibili 会员购抢票助手  v0.1.0".center(48) + "║")
    print("║" + "   仅供个人学习与研究使用，严禁商业牟利".center(48) + "║")
    print("╚" + "═" * 58 + "╝")
    print()


def print_menu():
    print("\n┌── 功能菜单 ─────────────────────────────┐")
    for key, label, _ in MENU:
        print(f"│  [{key}] {label:<38} │")
    print("└──────────────────────────────────────────┘")


def main():
    print_banner()

    # ── 初始化 ──
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.yaml"
    )
    config = Config(config_path)

    setup_logging(config)

    cache_dir = config.get("storage.kv_cache_dir", "./data/cache")
    cache = KVCache(cache_dir)

    client = BilibiliClient(config, cache)
    monitor = TicketMonitor(client, config, cache)
    executor = OrderExecutor(client, config, cache)

    # 注册到全局
    _components["config"] = config
    _components["cache"] = cache
    _components["client"] = client
    _components["monitor"] = monitor
    _components["executor"] = executor

    # 检查登录状态
    is_login, user_info = client.check_login()
    if is_login and user_info:
        uname = user_info.get("uname", "未知")
        print(f"👋 欢迎回来，{uname}！\n")
    else:
        print("⚠️  尚未登录，请使用菜单选项 2 或 3 登录\n")

    # ── 主循环 ──
    while True:
        print_menu()
        choice = input("\n请选择操作: ").strip()

        if choice == "0":
            print("\n正在保存缓存...")
            cache.flush()
            print("再见！")
            break

        matched = False
        for key, label, action in MENU:
            if key == choice and action is not None:
                action()
                matched = True
                break

        if not matched:
            print(f"\n无效选项: {choice}")


if __name__ == "__main__":
    main()
