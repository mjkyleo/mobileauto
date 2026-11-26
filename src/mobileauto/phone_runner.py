"""纯手机端脚本（Termux）：基于 uiautomator2 的 UI 文字识别，不使用 OCR/cv2/adb 命令。"""

import os
import sys
import time
import subprocess
import importlib
import importlib.util
import atexit
from datetime import datetime

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        _reconfigure(encoding="utf-8")


def _ensure_uiautomator2():
    try:
        if importlib.util.find_spec("uiautomator2") is not None:
            return
    except (ImportError, ValueError):
        pass

    print("缺少 uiautomator2，尝试自动安装...")
    cmd = [sys.executable, "-m", "pip", "install", "uiautomator2"]
    index_url = os.environ.get("PIP_INDEX_URL")
    if index_url:
        cmd += ["-i", index_url]
    print("   >>> " + " ".join(cmd))
    if subprocess.run(cmd).returncode != 0:
        print("❌ 自动安装失败。请在 Termux 里先执行：")
        print("   pkg install python libxml2 libxslt libjpeg-turbo")
        print("   pip install uiautomator2")
        sys.exit(1)
    importlib.invalidate_caches()
    print("✅ uiautomator2 安装完成。")


_ensure_uiautomator2()

import uiautomator2 as u2

# ================== 配置区 ==================
# atx-agent 地址。Termux 在本机运行时通常就是 http://127.0.0.1:7912
ATX_AGENT_URL = os.environ.get("ATX_AGENT_URL", "http://127.0.0.1:7912")
# 目标按钮文字（按优先级）
TARGETS = ["领取奖励", "继续观看", "继续互动", "领取成功"]
POLL_INTERVAL = 1.2
CLICK_COOLDOWN = 2.0
MISS_BACK_LIMIT = 5
CLICK_DEDUPE_WINDOW = 2.5
# 「领取成功」是状态提示，默认不点击文字本身
CLICK_SUCCESS_BANNER = False
# 点击提示条右侧的 X 关闭按钮（相对屏幕比例）
CLOSE_SUCCESS_BANNER = True
SUCCESS_CLOSE_X_RATIO = 0.915
SUCCESS_CLOSE_Y_RATIO = 0.070
# 自动打开目标 App
AUTO_START_APP = False
APP_PACKAGE = "com.luna.music"
# 回到主页后是否尝试自动点击入口进入任务页
AUTO_NAVIGATE_HOME = False
STOP_ON_HOME = True
HOME_ENTRY_KEYWORDS = ["福利中心", "免费听时长"]
# 这些词出现时不认为是按钮
BLOCK_WORDS = ["秒", "解锁", "后再", "再看", "提前", "完成", "互动", "任务", "福利", "第"]

# ===== 分层定位策略 L0-L4 =====
# 手机版没有 OCR/cv2，L2 模板匹配与 L3 OCR 会自动跳过，走 L0 控件属性 → L1 控件文字 → L4 比例坐标
ENABLE_LAYERED = True
LOG_HIT_LEVEL = True
# L0：resource-id 关键词映射，如 {"领取奖励": ["btn_reward"]}
TARGET_IDS = {}
# L0：content-desc 映射，默认自动把目标文字作为精确值
TARGET_DESCS = {}
# L4：相对位置兜底
TARGET_RATIO = {
    "领取奖励": (0.5, 0.52),
    "继续观看": (0.5, 0.52),
    "继续互动": (0.5, 0.52),
}
DEBUG_MAX_FILES = DEBUG_KEEP_MAX
DEBUG_MAX_DAYS = DEBUG_KEEP_DAYS
# ===========================================

from mobileauto.paths import (
    RUNTIME_DIR,
    DEBUG_UI_DIR,
    DEBUG_PHONE_DIR,
    TEMPLATE_DIR,
    DEBUG_KEEP_DAYS,
    DEBUG_KEEP_MAX,
    ensure_runtime_dirs,
)
from mobileauto.inspector import parse_hierarchy
from mobileauto.locator import LayeredLocator
from mobileauto.cleanup import prune_files

ensure_runtime_dirs()
LOG_FILE = os.path.join(RUNTIME_DIR, "mobileauto__phone.log")
DEBUG_DIR = os.path.join(RUNTIME_DIR, "debug_phone")
DEBUG_DIR_UI = DEBUG_UI_DIR


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            try:
                stream.write(data)
            except Exception:
                pass

    def flush(self):
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self):
        return any(getattr(s, "isatty", lambda: False)() for s in self.streams)


try:
    _log_fp = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    _log_fp.write(f"\n===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 启动（手机版）=====\n")
    sys.stdout = _Tee(sys.stdout, _log_fp)
    sys.stderr = _Tee(sys.stderr, _log_fp)
    atexit.register(_log_fp.close)
except Exception as e:
    LOG_FILE = None
    print(f"⚠️ 无法创建日志文件: {e}")

device = None
locator = None
last_click = {}
last_status = None


def connect_device():
    """优先用 atx-agent HTTP 地址连接，兼容不同 uiautomator2 版本。"""
    global device
    attempts = [
        ("ATX_AGENT_URL", lambda: u2.connect(ATX_AGENT_URL)),
        ("HTTP 默认地址", lambda: u2.connect("http://127.0.0.1:7912")),
        ("地址形式", lambda: u2.connect("127.0.0.1:7912")),
        ("自动检测", lambda: u2.connect()),
    ]
    last_error = None
    for name, func in attempts:
        try:
            device = func()
            print(f"✅ 连接成功（{name}）: {ATX_AGENT_URL}")
            init_locator()
            return device
        except Exception as e:
            last_error = e
    print("❌ 无法连接手机端 uiautomator2 服务")
    print(f"   最后一次错误: {last_error}")
    print("   请确认至少在电脑上执行一次：python -m uiautomator2 init")
    print("   并在手机设置里给 atx-agent / uiautomator 开启无障碍服务。")
    print("   也可设置环境变量 ATX_AGENT_URL=http://127.0.0.1:7912 后重试。")
    sys.exit(1)


def init_locator():
    """初始化分层定位器：手机版仅 L0/L1/L4 有效。"""
    global locator
    ratios = dict(TARGET_RATIO)
    ratios["领取成功_X"] = (SUCCESS_CLOSE_X_RATIO, SUCCESS_CLOSE_Y_RATIO)
    locator = LayeredLocator(
        TARGETS,
        target_ids=TARGET_IDS,
        target_descs=TARGET_DESCS,
        target_ratio=ratios,
        template_dir=TEMPLATE_DIR,
        log=print,
    )


def get_ui_rows():
    """读取 UI 层级供 L0/L1 使用；失败返回空列表，自动降级到 L4。"""
    try:
        return parse_hierarchy(device.dump_hierarchy())
    except Exception:
        return []


def click_hit(hit):
    """按分层定位结果点击并记录截图；去重已在 locator 内完成。"""
    os.makedirs(DEBUG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shot = os.path.join(DEBUG_DIR, f"{ts}_{hit.target}_{hit.x}_{hit.y}.png")
    try:
        device.screenshot(shot)
        prune_files(DEBUG_DIR, DEBUG_MAX_FILES, DEBUG_MAX_DAYS)
    except Exception:
        pass
    print(
        f">>> 点击 {hit.target} [{hit.level}/{hit.source}] "
        f"「{hit.label}」 @({hit.x},{hit.y}) | 截图 {shot}"
    )
    try:
        device.click(hit.x, hit.y)
    except Exception as e:
        print(f"⚠️ 点击失败: {e}")
    return True


def obj_text(obj):
    try:
        return str(obj.info.get("text", "") or "")
    except Exception:
        return ""


def blocked(text, target=""):
    if target and text == target:
        return False
    return any(word in text for word in BLOCK_WORDS)


def find_target_obj(target):
    """先精确匹配，再做短文本包含匹配。"""
    exact = device(text=target)
    if exact.exists and not blocked(obj_text(exact), target):
        return exact

    contains = device(textContains=target)
    if contains.exists:
        text = obj_text(contains)
        if not blocked(text, target) and len(text) <= len(target) + 2:
            return contains
    return None


def click_obj(obj, target):
    try:
        x, y = obj.center()
    except Exception:
        return False

    key = (target, x // 20, y // 20)
    now = time.time()
    if now - last_click.get(key, 0) < CLICK_DEDUPE_WINDOW:
        print(f"· 跳过重复点击「{target}」@({x},{y})")
        return True
    last_click[key] = now

    os.makedirs(DEBUG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shot = os.path.join(DEBUG_DIR, f"{ts}_{target}_{x}_{y}.png")
    try:
        device.screenshot(shot)
        prune_files(DEBUG_DIR, DEBUG_MAX_FILES, DEBUG_MAX_DAYS)
    except Exception:
        pass

    text = obj_text(obj)
    print(f">>> 点击 {target} | 「{text}」 @({x},{y}) | 截图 {shot}")
    try:
        obj.click()
    except Exception:
        device.click(x, y)
    return True


def screen_size():
    try:
        info = device.info or {}
        return int(info.get("displayWidth", 0) or 0), int(info.get("displayHeight", 0) or 0)
    except Exception:
        return 0, 0


def find_success_close_obj():
    """在右上角找「领取成功」提示条旁边的 X 图标。"""
    width, height = screen_size()
    if not width or not height:
        return None
    target_x = int(width * SUCCESS_CLOSE_X_RATIO)
    target_y = int(height * SUCCESS_CLOSE_Y_RATIO)
    dx = max(24, int(width * 0.03))
    dy = max(24, int(height * 0.03))

    selector = device(className="android.widget.ImageView")
    try:
        count = selector.count
    except Exception:
        return None

    for i in range(count):
        obj = device(className="android.widget.ImageView", instance=i)
        try:
            x, y = obj.center()
            if abs(x - target_x) <= dx and abs(y - target_y) <= dy:
                return obj, x, y
        except Exception:
            continue
    return None


def click_success_close():
    found = find_success_close_obj()
    if found is None:
        return False
    obj, x, y = found
    print(f"· 点击「领取成功」右侧 X @({x},{y})")
    return click_obj(obj, "领取成功X")


def dump_ui(reason):
    os.makedirs(DEBUG_DIR_UI, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(DEBUG_DIR_UI, f"{ts}_{reason}.xml")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(device.dump_hierarchy())
        prune_files(DEBUG_DIR_UI, DEBUG_MAX_FILES, DEBUG_MAX_DAYS)
        print(f"· 已保存 UI 层级: {path}")
    except Exception as e:
        print(f"⚠️ 保存 UI 层级失败: {e}")


def is_countdown():
    return (
        device(textContains="秒后可领取奖励").exists
        or device(textContains="继续解锁").exists
        or device(textContains="再看").exists
    )


def is_home_page():
    return device(text="福利中心").exists and device(text="免费听时长").exists


def is_live():
    height = 0
    try:
        height = int((device.info or {}).get("displayHeight", 0) or 0)
    except Exception:
        pass
    for keyword in ("关注", "更多直播"):
        obj = device(text=keyword)
        if obj.exists:
            try:
                _, y = obj.center()
                if not height or y < height * 0.25:
                    return True
            except Exception:
                return True
    return False


def click_home_entry():
    for keyword in HOME_ENTRY_KEYWORDS:
        obj = device(textContains=keyword)
        if obj.exists:
            print(f"· 自动进入任务页: 「{obj_text(obj)}」")
            click_obj(obj, keyword)
            return True
    return False


print("🚀 自动化脚本启动（纯手机 Termux / UI 文字识别）")
print(f"   atx-agent: {ATX_AGENT_URL}")
print(f"   目标: {TARGETS}  轮询={POLL_INTERVAL}s")
print(f"   日志文件: {LOG_FILE if LOG_FILE else '未启用'}")
connect_device()

device.implicitly_wait(1)

if AUTO_START_APP:
    print(f"· 自动启动 App: {APP_PACKAGE}")
    try:
        device.app_start(APP_PACKAGE)
        time.sleep(3)
    except Exception as e:
        print(f"⚠️ 自动启动失败: {e}")

miss_count = 0
while True:
    try:
        if is_countdown():
            if last_status != "countdown":
                print("· 广告倒计时中，等待目标出现")
                last_status = "countdown"
            miss_count = 0
            time.sleep(POLL_INTERVAL)
            continue
        if last_status == "countdown":
            print("· 倒计时结束，恢复识别")
            last_status = None

        if is_home_page():
            if AUTO_NAVIGATE_HOME and click_home_entry():
                miss_count = 0
                time.sleep(2)
                continue
            if STOP_ON_HOME:
                print("✅ 已回到主页面，脚本停止")
                break

        if is_live():
            print("⚡ 检测到直播间顶部特征，秒退")
            device.press("back")
            time.sleep(1.5)
            continue

        rows = get_ui_rows() if ENABLE_LAYERED else []
        sw, sh = screen_size()

        clicked = None
        for target in TARGETS:
            if target == "领取成功" and not CLICK_SUCCESS_BANNER:
                probe = locator.locate(target, rows=rows, sw=sw, sh=sh)
                if not probe:
                    continue
                close_hit = locator.locate_success_close(
                    rows=rows, sw=sw, sh=sh, dedupe_window=CLICK_DEDUPE_WINDOW
                )
                if close_hit:
                    if LOG_HIT_LEVEL:
                        print(f"· 命中层级: {close_hit.describe()}")
                    click_hit(close_hit)
                elif CLOSE_SUCCESS_BANNER:
                    click_success_close()
                clicked = target
                break

            hit = locator.locate(
                target, rows=rows, sw=sw, sh=sh, dedupe_window=CLICK_DEDUPE_WINDOW
            )
            if not hit:
                continue
            if LOG_HIT_LEVEL:
                print(f"· 命中层级: {hit.describe()}")
            click_hit(hit)
            clicked = target
            break

        if clicked is None and CLOSE_SUCCESS_BANNER:
            if click_success_close():
                clicked = "领取成功X"

        if clicked:
            miss_count = 0
            if clicked == "领取成功":
                continue
            time.sleep(CLICK_COOLDOWN)
            continue

        time.sleep(POLL_INTERVAL)
        miss_count += 1
        if miss_count == 1 or miss_count % 3 == 0:
            print(f"· 未找到目标（连续 {miss_count} 次）")
        if miss_count >= MISS_BACK_LIMIT:
            dump_ui("连续未匹配")
            print("⚠️ 连续未匹配，尝试返回上一页")
            device.press("back")
            miss_count = 0
            time.sleep(2)

    except KeyboardInterrupt:
        print("\n已停止")
        break
    except Exception as e:
        print(f"⚠️ 错误: {e}")
        time.sleep(5)
