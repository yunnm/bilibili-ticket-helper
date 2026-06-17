"""
Flask 后端 API 服务器

提供所有抢票功能的 REST API 接口，供前端页面调用。
"""

import sys
import os
import json
import time
import logging
import threading
import io
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify, send_from_directory
from src.config import Config
from src.kv_cache import KVCache
from src.bilibili_api import BilibiliClient
from src.monitor import TicketMonitor, TicketStatus
from src.order import OrderExecutor, OrderStatus


# ── 初始化 ──────────────────────────────────────────────────

app = Flask(__name__, static_folder='web', static_url_path='')

config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
config = Config(config_path)
cache = KVCache(config.get("storage.kv_cache_dir", "./data/cache"))
client = BilibiliClient(config, cache)
monitor: TicketMonitor = None
executor: OrderExecutor = None

# 日志捕获
log_buffer = io.StringIO()
log_handler = logging.StreamHandler(log_buffer)
log_handler.setLevel(logging.INFO)
log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(log_handler)
logging.getLogger("monitor").setLevel(logging.DEBUG)
logging.getLogger("order").setLevel(logging.DEBUG)

_monitor_thread = None
_monitor_running = False


def _init_executor():
    global executor, monitor
    executor = OrderExecutor(client, config, cache)
    monitor = TicketMonitor(client, config, cache)
    # 绑定自动下单回调
    monitor.on_available = _on_ticket_available
    monitor.on_status_change = _on_status_change


def _on_ticket_available(detail):
    """监控检测到有票时的回调"""
    if executor:
        result = executor.execute()
        if result.is_success:
            logging.getLogger("order").info("🎉 自动抢票成功！订单号: %s", result.order_id)
        else:
            logging.getLogger("order").warning("自动下单失败: %s", result.message)


def _on_status_change(old, new, detail):
    """状态变更回调"""
    logging.getLogger("monitor").info("状态变更: %s → %s", old.name, new.name)


_init_executor()


# ── 静态文件 ────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


# ── 状态接口 ────────────────────────────────────────────────

@app.route('/api/status')
def api_status():
    """获取当前全局状态"""
    is_login, user_info = client.check_login()
    return jsonify({
        "logged_in": is_login,
        "user": {
            "uname": user_info.get("uname", "") if user_info else "",
            "mid": user_info.get("mid", "") if user_info else "",
            "level": user_info.get("level_info", {}).get("current_level", "") if user_info else "",
        } if user_info else None,
        "config": {
            "item_id": config.get("ticket.item_id", ""),
            "sku_id": config.get("ticket.sku_id", ""),
            "screen_id": config.get("ticket.screen_id", ""),
            "count": config.get("ticket.count", 1),
        },
        "monitor_running": _monitor_running,
        "monitor_stats": monitor.get_stats() if monitor else {},
        "last_order": _get_last_order_info(),
    })


def _get_last_order_info():
    if not executor:
        return None
    last = executor.get_last_result()
    if not last:
        return None
    return {
        "status": last.status.name,
        "order_id": last.order_id,
        "message": last.message,
        "attempts": executor.get_attempt_count(),
    }


# ── 登录接口 ────────────────────────────────────────────────

@app.route('/api/login/check', methods=['POST'])
def api_login_check():
    """检查登录状态"""
    is_login, user_info = client.check_login()
    return jsonify({
        "logged_in": is_login,
        "user": {
            "uname": user_info.get("uname", ""),
            "mid": user_info.get("mid", ""),
        } if user_info else None,
    })


@app.route('/api/login/cookie', methods=['POST'])
def api_login_cookie():
    """通过 Cookie 登录"""
    data = request.get_json()
    cookie_str = data.get("cookie", "").strip()
    if not cookie_str:
        return jsonify({"success": False, "error": "Cookie 不能为空"}), 400

    client.set_cookie(cookie_str)
    is_login, user_info = client.check_login()

    if is_login:
        return jsonify({
            "success": True,
            "user": {
                "uname": user_info.get("uname", ""),
                "mid": user_info.get("mid", ""),
            }
        })
    else:
        return jsonify({"success": False, "error": "Cookie 无效或已过期"})


@app.route('/api/login/qrcode', methods=['POST'])
def api_login_qrcode():
    import qrcode as qrcode_lib
    
    qr_result = client.generate_qrcode()
    if qr_result is None:
        return jsonify({"success": False, "error": "???????"}), 500
    
    qrcode_key = qr_result["qrcode_key"]
    qr_url = qr_result["url"]
    
    qr = qrcode_lib.QRCode(border=2)
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    import base64
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    
    return jsonify({
        "success": True,
        "qrcode_key": qrcode_key,
        "qr_image": f"data:image/png;base64,{qr_b64}",
    })

@app.route('/api/login/qrcode/check', methods=['POST'])
def api_login_qrcode_check():
    """检查扫码登录结果"""
    data = request.get_json()
    qrcode_key = data.get("qrcode_key", "")

    resp = client.poll_qrcode(qrcode_key)
    if resp is None:
        return jsonify({"status": "error", "message": "网络错误"})

    code = resp.get("code")
    if code == 0:
        cookie_str = client._extract_cookie_string()
        cache.set("session.cookie", cookie_str)
        cache.flush()
        is_login, user_info = client.check_login()
        return jsonify({
            "status": "success",
            "user": {
                "uname": user_info.get("uname", ""),
                "mid": user_info.get("mid", ""),
            } if user_info else None,
        })
    elif code == 86038:
        return jsonify({"status": "expired", "message": "二维码已过期"})
    elif code == 86090:
        return jsonify({"status": "waiting_confirm", "message": "已扫码，请在手机上确认"})
    elif code == 86101:
        return jsonify({"status": "waiting_scan", "message": "等待扫码"})
    else:
        return jsonify({"status": "waiting_scan", "message": f"状态码: {code}"})


# ── 商品查询接口 ────────────────────────────────────────────

@app.route('/api/item/lookup', methods=['POST'])
def api_item_lookup():
    """查询商品详情和 SKU 列表"""
    data = request.get_json()
    item_id = data.get("item_id", "").strip()
    if not item_id:
        return jsonify({"success": False, "error": "请输入商品 ID"}), 400

    detail = client.get_project_detail(item_id)
    if not detail or detail.get("code") != 0:
        return jsonify({"success": False, "error": "无法获取商品信息，请检查 ID"}), 404

    proj_data = detail.get("data", {})
    name = proj_data.get("name", proj_data.get("project_name", "未知"))

    # 提取 SKU
    skus_raw = client.extract_skus(detail)
    skus = []
    for sku in skus_raw:
        skus.append({
            "sku_id": str(sku.get("sku_id", sku.get("id", ""))),
            "name": sku.get("name", sku.get("sku_name", "")),
            "price": sku.get("price", sku.get("real_price", "")),
            "stock": sku.get("sale_count", sku.get("stock", "?")),
        })

    # 提取场次
    screens_raw = proj_data.get("screen_list", [])
    screens = []
    for s in screens_raw:
        screens.append({
            "screen_id": str(s.get("screen_id", s.get("id", ""))),
            "name": s.get("name", s.get("screen_name", "")),
        })

    return jsonify({
        "success": True,
        "item": {
            "item_id": item_id,
            "name": name,
            "skus": skus,
            "screens": screens,
        }
    })


# ── 配置接口 ────────────────────────────────────────────────

@app.route('/api/config', methods=['POST'])
def api_config_update():
    """更新抢票配置"""
    data = request.get_json()
    for key, value in data.items():
        config.set(f"ticket.{key}", value)

    # 重建 executor 和 monitor
    global executor, monitor
    executor = OrderExecutor(client, config, cache)
    monitor = TicketMonitor(client, config, cache)
    monitor.on_available = _on_ticket_available
    monitor.on_status_change = _on_status_change

    return jsonify({"success": True})


# ── 监控接口 ────────────────────────────────────────────────

@app.route('/api/monitor/start', methods=['POST'])
def api_monitor_start():
    """启动后台监控"""
    global _monitor_running

    if not config.get("ticket.item_id"):
        return jsonify({"success": False, "error": "请先设置抢票目标"}), 400
    if not config.get("ticket.sku_id"):
        return jsonify({"success": False, "error": "请先选择 SKU"}), 400

    if _monitor_running:
        return jsonify({"success": False, "error": "监控已在运行中"}), 400

    _monitor_running = True
    monitor.start()
    logging.getLogger("monitor").info("后台监控已启动")
    return jsonify({"success": True, "message": "监控已启动"})


@app.route('/api/monitor/stop', methods=['POST'])
def api_monitor_stop():
    """停止后台监控"""
    global _monitor_running
    _monitor_running = False
    if monitor:
        monitor.stop()
    logging.getLogger("monitor").info("后台监控已停止")
    return jsonify({"success": True, "message": "监控已停止"})


@app.route('/api/monitor/status')
def api_monitor_status():
    """获取监控实时状态"""
    if not monitor:
        return jsonify({"running": False, "stats": {}})
    return jsonify({
        "running": _monitor_running,
        "stats": monitor.get_stats(),
    })


# ── 下单接口 ────────────────────────────────────────────────

@app.route('/api/order/execute', methods=['POST'])
def api_order_execute():
    """手动执行下单"""
    if not config.get("ticket.item_id"):
        return jsonify({"success": False, "error": "请先设置抢票目标"}), 400

    result = executor.execute()
    return jsonify({
        "success": result.is_success,
        "status": result.status.name,
        "order_id": result.order_id,
        "message": result.message,
    })


# ── 日志接口 ────────────────────────────────────────────────

@app.route('/api/logs')
def api_logs():
    """获取最近的日志"""
    log_buffer.seek(0)
    lines = log_buffer.getvalue().strip().split("\n")
    # 返回最近 100 行
    recent = lines[-100:] if len(lines) > 100 else lines
    return jsonify({"logs": recent})


# ── 启动入口 ────────────────────────────────────────────────

def run_server(host="127.0.0.1", port=5199, open_browser=True):
    """启动 Flask 服务器"""
    if open_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    # 禁用 Flask 的 banner
    import logging as flask_logging
    flask_log = flask_logging.getLogger('werkzeug')
    flask_log.setLevel(flask_logging.WARNING)

    print(f"\n  抢票助手 GUI 已启动 → http://{host}:{port}")
    print(f"  浏览器将自动打开，如未打开请手动访问上述地址\n")

    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == '__main__':
    run_server()
