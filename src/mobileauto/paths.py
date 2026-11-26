"""统一路径解析：源码运行和 PyInstaller 打包后都可用。"""

import os
import sys


def get_app_dir():
    """返回程序根目录：源码模式是项目根目录，exe 模式是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(here))


APP_DIR = get_app_dir()
RUNTIME_DIR = os.path.join(APP_DIR, "runtime")


def get_web_dir():
    """Web 静态资源目录，兼容 PyInstaller 打包后的资源路径。"""
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "src", "mobileauto", "web")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


WEB_DIR = get_web_dir()
WEB_INDEX = os.path.join(WEB_DIR, "index.html")

DEBUG_CLICKS_DIR = os.path.join(RUNTIME_DIR, "debug_clicks")
DEBUG_OCR_DIR = os.path.join(RUNTIME_DIR, "debug_ocr")
DEBUG_UI_DIR = os.path.join(RUNTIME_DIR, "debug_ui")
DEBUG_PHONE_DIR = os.path.join(RUNTIME_DIR, "debug_phone")
PAGE_DATA_DIR = os.path.join(RUNTIME_DIR, "page_data")

# 阶段2：PC 定制的规则包与手机回传的执行报告
RULES_DIR = os.path.join(APP_DIR, "rules")
REPORTS_DIR = os.path.join(RUNTIME_DIR, "reports")

# 分层定位 L2 模板目录：把按钮小图按目标名放进来即可启用（如 领取奖励.png）
TEMPLATE_DIR = os.path.join(APP_DIR, "templates")

# 图片/记录清理策略：调试图片按天数与数量双重上限，采集按批次数量上限
DEBUG_KEEP_DAYS = 3
DEBUG_KEEP_MAX = 60
COLLECT_KEEP_MAX = 8


def ensure_runtime_dirs():
    for path in (
        RUNTIME_DIR,
        DEBUG_CLICKS_DIR,
        DEBUG_OCR_DIR,
        DEBUG_UI_DIR,
        DEBUG_PHONE_DIR,
        PAGE_DATA_DIR,
        REPORTS_DIR,
        RULES_DIR,
    ):
        os.makedirs(path, exist_ok=True)
