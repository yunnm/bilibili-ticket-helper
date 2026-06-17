"""
桌面启动入口

双击此脚本即可启动抢票助手 GUI。
会自动检测依赖并启动 Flask 后端 + 前端界面。
"""

import sys
import os
import subprocess

# 确保项目根在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def check_dependencies():
    """检查并安装缺失的依赖"""
    deps = ["flask", "requests", "pyyaml", "qrcode"]
    missing = []

    for dep in deps:
        try:
            __import__(dep)
        except ImportError:
            missing.append(dep)

    if missing:
        print(f"检测到缺失依赖: {', '.join(missing)}")
        print("正在自动安装...")
        for dep in missing:
            subprocess.check_call([sys.executable, "-m", "pip", "install", dep, "-q"])
        print("依赖安装完成!\n")

    try:
        from PIL import Image
    except ImportError:
        print("正在安装 Pillow...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow", "-q"])


def main():
    print("=" * 50)
    print("  Bilibili 会员购抢票助手")
    print("  仅供个人学习与研究使用，严禁商业牟利")
    print("=" * 50)

    check_dependencies()

    from src.app import run_server
    run_server(open_browser=True)


if __name__ == "__main__":
    main()
