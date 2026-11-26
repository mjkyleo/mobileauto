import os
import sys
import time
import random
import re
import subprocess
import importlib
import importlib.util
import atexit
from datetime import datetime
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        _reconfigure(encoding="utf-8")


from mobileauto.paths import (
    APP_DIR as SCRIPT_DIR,
    RUNTIME_DIR,
    DEBUG_CLICKS_DIR,
    DEBUG_OCR_DIR,
    TEMPLATE_DIR,
    DEBUG_KEEP_DAYS,
    DEBUG_KEEP_MAX,
    ensure_runtime_dirs,
)
from mobileauto.inspector import parse_hierarchy
from mobileauto.locator import LayeredLocator
from mobileauto.cleanup import prune_files

ensure_runtime_dirs()
LOG_FILE = os.path.join(RUNTIME_DIR, "mobileauto__u2.log")


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
    _log_fp.write(f"\n===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
    sys.stdout = _Tee(sys.stdout, _log_fp)
    sys.stderr = _Tee(sys.stderr, _log_fp)
    atexit.register(_log_fp.close)
except Exception as e:
    LOG_FILE = None
    print(f"⚠️ 无法创建日志文件: {e}")


def _ensure_dependencies():
    android = sys.platform == "linux" and (
        "android" in sys.version.lower() or os.path.exists("/system/bin/getprop")
    )
    required = [
        ("cv2", "opencv-python-headless"),
        ("numpy", "numpy"),
        ("rapidocr_onnxruntime", "rapidocr-onnxruntime"),
        ("uiautomator2", "uiautomator2"),
    ]
    missing = []
    for module_name, package_name in required:
        try:
            if importlib.util.find_spec(module_name) is None:
                missing.append((module_name, package_name))
        except (ImportError, ValueError):
            missing.append((module_name, package_name))
    if not missing:
        return

    print("=" * 60)
    print("检测到缺少依赖，开始自动安装：")
    for module_name, package_name in missing:
        print(f"   {module_name:<24} -> pip install {package_name}")
    print("=" * 60)

    if android:
        print("提示：Pydroid3 若安装失败，请先安装 Pydroid repository plugin，")
        print("      或在 Pydroid3 的 Pip 界面勾选预编译包后重试。")

    for module_name, package_name in missing:
        cmd = [sys.executable, "-m", "pip", "install", package_name]
        if android:
            cmd.append("--only-binary=:all:")
        index_url = os.environ.get("PIP_INDEX_URL")
        if index_url:
            cmd += ["-i", index_url]
        print(f"   >>> {' '.join(cmd)}")
        if subprocess.run(cmd).returncode != 0:
            print(f"❌ 自动安装 {package_name} 失败。")
            if android:
                print("   Pydroid3 上普通 pip 通常装不了原生库，请按下面步骤手动装：")
                print("   1. Google Play 安装 Pydroid repository plugin")
                print("   2. Pydroid3 菜单 -> Pip -> INSTALL")
                print(f"   3. 搜索 {package_name}，勾选 Use prebuilt wheels from Pydroid repository 后安装")
                print("   4. 装完重新运行脚本")
            else:
                print(f"   请手动执行: python -m pip install {package_name}")
            sys.exit(1)

    importlib.invalidate_caches()
    still_missing = []
    for module_name, package_name in missing:
        try:
            if importlib.util.find_spec(module_name) is None:
                still_missing.append(package_name)
        except (ImportError, ValueError):
            still_missing.append(package_name)
    if still_missing:
        print("❌ 安装完成但仍无法导入：" + ", ".join(still_missing))
        print("   请重启 Pydroid3 / 命令行后重试，或手动安装。")
        sys.exit(1)
    print("✅ 依赖安装完成，继续启动脚本。")


_ensure_dependencies()

import cv2
import numpy as np
import uiautomator2 as u2
from rapidocr_onnxruntime import RapidOCR

# ================== 配置区 ==================
# 目标文字（按优先级：先点靠前的）
TARGETS = ["领取奖励", "继续观看", "继续互动", "领取成功"]
# 置信度下限
OCR_SCORE = 0.5
# 允许的轻微识别误差（去空格后做包含/近似）
FUZZY = True
POLL_INTERVAL = 1.2
CLICK_COOLDOWN = 2.0
# 未命中时是否随机上滑（默认关，避免误滑干扰排查）
ENABLE_RANDOM_SWIPE = False
SWIPE_PROB = 0.12
# 各目标允许点击的区域（相对屏幕宽高 0~1）
# 「领取奖励」「继续观看」「继续互动」必须在屏幕中央；「领取成功」常见右上角
TARGET_ROI = {
    "领取奖励": {"x": (0.25, 0.75), "y": (0.35, 0.70)},  # 中央
    "继续观看": {"x": (0.25, 0.75), "y": (0.35, 0.70)},  # 中央
    "继续互动": {"x": (0.25, 0.75), "y": (0.35, 0.70)},  # 中央
    "领取成功": {"x": (0.0, 1.0), "y": (0.01, 0.97)},    # 几乎全屏（含右上）
}
CENTER_TARGETS = {"领取奖励", "继续观看", "继续互动"}
# 截图过大时缩小再 OCR，加快速度（坐标会按比例还原）
OCR_MAX_WIDTH = 720
# 每次点击保存标注截图，方便排查误点
SAVE_CLICK_DEBUG = True
# 连续未匹配多少次才执行返回（倒计时页面不会计入）
MISS_BACK_LIMIT = 5
# 同一目标/位置在多少秒内不重复点击
CLICK_DEDUPE_WINDOW = 2.5
# 「领取成功」是状态提示，默认不点击文字本身
CLICK_SUCCESS_BANNER = False
# 改为点击提示条右侧的 X 关闭按钮（相对屏幕比例）
CLOSE_SUCCESS_BANNER = True
SUCCESS_CLOSE_X_RATIO = 0.915
SUCCESS_CLOSE_Y_RATIO = 0.070
# 回到主页面后停止脚本，避免继续乱按返回
STOP_ON_HOME = True
# 是否把每一帧 OCR 都打印出来（默认关闭，减少日志噪音）
LOG_OCR_EVERY_FRAME = False
# 自动打开目标 App 并尝试进入任务页
AUTO_START_APP = True
APP_PACKAGE = "com.luna.music"
AUTO_NAVIGATE_HOME = True
HOME_ENTRY_KEYWORDS = ["福利", "领时长", "福利中心", "免费听时长"]
# 只在目标 App 前台时操作，避免停留在微信等别的 App 时盲点
ONLY_TARGET_APP = True
# 可操作特征：必须真正识别到按钮文字才允许点击；「广告」只是页面标识，不算可操作
ACTION_KEYWORDS = ["领取奖励", "立即领取", "继续观看", "继续互动", "再看", "领取成功"]
# 设备地址：留空自动检测；可填 USB 序列号或 adb WiFi 地址，如 "192.168.1.100:5555"
# 注意：uiautomator2 的连接初始化仍依赖 adb/adbutils，脚本本身不直接调用 adb 命令。
DEVICE_SERIAL = os.environ.get("DEVICE_SERIAL") or os.environ.get("ANDROID_SERIAL", "")

# ===== 分层定位策略 L0-L4 =====
# L0 控件ID/稳定属性 → L1 控件文本 → L2 模板匹配 → L3 OCR → L4 比例坐标
ENABLE_LAYERED = True
LOG_HIT_LEVEL = True  # 打印每次命中的层级，便于确认走了哪一级
# L0：resource-id 关键词映射。留空时仅用 content-desc 命中。
# 例：{"领取奖励": ["btn_reward", "claim"]}
TARGET_IDS = {}
# L0：content-desc 映射。默认已自动把目标文字作为精确值。
TARGET_DESCS = {}
# L2：模板匹配阈值与目录（把按钮小图命名为「领取奖励.png」放到 templates/ 即生效）
TEMPLATE_THRESHOLD = 0.85
# L4：按钮相对位置兜底（0~1）。只有前面都失败才会用。
TARGET_RATIO = {
    "领取奖励": (0.5, 0.554),
    "继续观看": (0.5, 0.554),
    "继续互动": (0.5, 0.554),
}
# ===== 图片清理 =====
DEBUG_MAX_FILES = DEBUG_KEEP_MAX  # 每个调试目录最多保留多少张/条
DEBUG_MAX_DAYS = DEBUG_KEEP_DAYS  # 超过多少天自动删除
# ===========================================

DEBUG_DIR = DEBUG_CLICKS_DIR
OCR_DUMP_DIR = DEBUG_OCR_DIR

ocr_engine = None
click_seq = 0
device: Any = None
locator = None
last_click = {}
recent_ocr = []
last_status = None


def get_ocr():
    global ocr_engine
    if ocr_engine is None:
        print("⏳ 加载 OCR 模型...")
        ocr_engine = RapidOCR()
        print("✅ OCR 就绪")
    return ocr_engine


def connect_device():
    """通过 uiautomator2 连接手机；脚本不调用 adb 命令，但初始化依赖 adb server。"""
    global device
    if DEVICE_SERIAL:
        print(f"🔌 连接设备: {DEVICE_SERIAL}")
    else:
        print("🔌 连接设备: 自动检测")

    try:
        device = u2.connect(DEVICE_SERIAL) if DEVICE_SERIAL else u2.connect()
    except Exception as e:
        print("❌ uiautomator2 连接设备失败")
        print(f"   原因: {e}")
        print("   请确认手机已开启 USB 调试并授权电脑，且 adb server 可用（adb devices 能看到设备）。")
        print("   首次连接会自动在手机上安装 uiautomator2/atx-agent 服务。")
        print("   也可设置环境变量 DEVICE_SERIAL 或 ANDROID_SERIAL 指定设备。")
        sys.exit(1)

    try:
        info = device.info or {}
        name = info.get("productName") or info.get("model") or info.get("serial") or "未知设备"
        width, height = info.get("displayWidth"), info.get("displayHeight")
        size = f" {width}x{height}" if width and height else ""
        print(f"✅ 设备已连接: {name}{size}")
    except Exception:
        print("✅ 设备已连接")
    init_locator()
    return device


def init_locator():
    """把配置注入分层定位器；成功提示的 X 走 L4 固定坐标。"""
    global locator
    ratios = dict(TARGET_RATIO)
    ratios["领取成功_X"] = (SUCCESS_CLOSE_X_RATIO, SUCCESS_CLOSE_Y_RATIO)
    locator = LayeredLocator(
        TARGETS,
        target_ids=TARGET_IDS,
        target_descs=TARGET_DESCS,
        target_roi=TARGET_ROI,
        target_ratio=ratios,
        template_dir=TEMPLATE_DIR,
        template_threshold=TEMPLATE_THRESHOLD,
        log=print,
    )


def get_ui_rows():
    """读取 UI 层级供 L0/L1 使用；失败返回空列表，自动降级到后续层级。"""
    try:
        return parse_hierarchy(device.dump_hierarchy())
    except Exception:
        return []


def screenshot():
    """通过 uiautomator2 截图，返回 OpenCV 使用的 BGR 图像。"""
    try:
        img = device.screenshot(format="opencv")
        if img is not None:
            return img
    except TypeError:
        pass
    except Exception as e:
        print(f"⚠️ 截图失败: {e}")
        return None

    # 兼容旧版本：screenshot() 返回 PIL.Image
    try:
        img = device.screenshot()
    except Exception as e:
        print(f"⚠️ 截图失败: {e}")
        return None
    if img is None:
        return None
    arr = np.array(img.convert("RGB"))
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def tap(x, y):
    device.click(int(x), int(y))


def press_back():
    device.press("back")


def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.strip().replace(" ", "").replace("\u3000", "")
    # 去掉 OCR 常见尾巴符号（如「领取成功×」）
    s = re.sub(r"[×xX✕✖×·•\|丨\[\]【】()（）\d:：]+$", "", s)
    s = re.sub(r"^[×xX✕✖·•\|丨]+", "", s)
    # 常见 OCR 混淆
    s = s.replace("领収", "领取").replace("领职", "领取").replace("领敢", "领取")
    s = s.replace("奨", "奖").replace("奬", "奖")
    s = s.replace("勵", "励")
    return s


def text_match(recognized: str, target: str) -> bool:
    """严格匹配按钮文字：只允许少量 OCR 噪声，不把长句/倒计时当按钮。"""
    a = normalize_text(recognized)
    b = normalize_text(target)
    if not a:
        return False
    core = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", a)
    if core == b:
        return True
    # 排除倒计时、提示句、说明文字；精确匹配已提前放行
    if any(k in a for k in ["秒", "解锁", "后可领取", "第", "再看", "提前得", "互动", "任务", "福利"]):
        return False
    if not FUZZY:
        return False
    # 只允许 1 个字的差异（OCR 误识别/多一个干扰字）
    if abs(len(core) - len(b)) > 1:
        return False
    if len(core) == len(b):
        return sum(1 for x, y in zip(core, b) if x != y) <= 1
    longer, shorter = (core, b) if len(core) > len(b) else (b, core)
    return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))


def is_countdown(items):
    """屏幕上是否还在广告倒计时。"""
    for text, score, cx, cy in items:
        norm = normalize_text(text)
        if re.search(r"\d+\s*秒", norm):
            return True
        if "秒后" in norm or "秒可" in norm:
            return True
    return False


def is_home_page(items):
    """是否已经回到主页面。"""
    home_keys = {"福利中心", "免费听时长", "金币收益", "现金收益"}
    hits = sum(1 for text, score, cx, cy in items if normalize_text(text) in home_keys)
    return hits >= 2


def record_ocr(items):
    recent_ocr.append((datetime.now().strftime("%H:%M:%S"), items))
    if len(recent_ocr) > 5:
        del recent_ocr[:-5]


def dump_ocr_snapshot(screen, reason="未匹配"):
    """把最近几帧 OCR 和当前截图存到脚本目录，便于后续优化。"""
    os.makedirs(OCR_DUMP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    txt_path = os.path.join(OCR_DUMP_DIR, f"{ts}_{reason}.txt")
    try:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"时间: {ts}\n原因: {reason}\n")
            for stamp, items in recent_ocr:
                f.write(f"\n[{stamp}]\n")
                for text, score, cx, cy in items:
                    f.write(f"  {text}\t{score:.3f}\t({int(cx)},{int(cy)})\n")
        cv2.imwrite(os.path.join(OCR_DUMP_DIR, f"{ts}_{reason}.png"), screen)
        prune_files(OCR_DUMP_DIR, DEBUG_MAX_FILES, DEBUG_MAX_DAYS)
    except Exception as e:
        print(f"⚠️ 保存 OCR 记录失败: {e}")
        return
    print(f"· 已保存 OCR 记录: {txt_path}")


def click_by_keyword(items, keywords):
    """在主页等场景中，按关键字点击入口。"""
    candidates = []
    for text, score, cx, cy in items:
        n = normalize_text(text)
        if any(k in n for k in keywords):
            candidates.append((score, cx, cy, text))
    if not candidates:
        return False
    candidates.sort(key=lambda c: -c[0])
    score, cx, cy, text = candidates[0]
    x, y = int(round(cx)), int(round(cy))
    print(f"· 自动进入任务页: 「{text}」 @({x},{y})")
    tap(x, y)
    return True


def box_center(box):
    """box: 4 点 [[x,y], ...] -> (cx, cy)"""
    pts = np.array(box, dtype=np.float32)
    cx = float(pts[:, 0].mean())
    cy = float(pts[:, 1].mean())
    return cx, cy


def in_roi(cx, cy, sw, sh, target: str):
    roi = TARGET_ROI.get(target, {"x": (0.0, 1.0), "y": (0.0, 1.0)})
    x0, x1 = roi["x"]
    y0, y1 = roi["y"]
    return x0 * sw <= cx <= x1 * sw and y0 * sh <= cy <= y1 * sh


def pos_pct(cx, cy, sw, sh):
    return cx / sw * 100.0, cy / sh * 100.0


def save_click_debug(screen, x, y, target, text, score, seq, ts):
    """保存带点击标记的截图，方便对照误点"""
    if not SAVE_CLICK_DEBUG:
        return None
    os.makedirs(DEBUG_DIR, exist_ok=True)
    vis = screen.copy()
    # 大红十字 + 圆，醒目
    cv2.drawMarker(vis, (x, y), (0, 0, 255), markerType=cv2.MARKER_CROSS, markerSize=60, thickness=3)
    cv2.circle(vis, (x, y), 28, (0, 0, 255), 3)
    cv2.circle(vis, (x, y), 6, (0, 255, 255), -1)
    # 画该目标 ROI 框
    sh, sw = screen.shape[:2]
    roi = TARGET_ROI.get(target, {"x": (0.0, 1.0), "y": (0.0, 1.0)})
    x0, y0 = int(roi["x"][0] * sw), int(roi["y"][0] * sh)
    x1, y1 = int(roi["x"][1] * sw), int(roi["y"][1] * sh)
    cv2.rectangle(vis, (x0, y0), (x1, y1), (0, 255, 0), 2)
    label = f"#{seq} {target} | OCR:{text} | {score:.3f} | ({x},{y})"
    cv2.rectangle(vis, (0, 0), (min(sw, 20 + 14 * len(label)), 48), (0, 0, 0), -1)
    cv2.putText(vis, label, (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    safe_target = re.sub(r"[^\w\u4e00-\u9fff]+", "_", target)
    path = os.path.join(DEBUG_DIR, f"{ts}_{seq:04d}_{safe_target}_{x}_{y}.png")
    cv2.imwrite(path, vis)
    prune_files(DEBUG_DIR, DEBUG_MAX_FILES, DEBUG_MAX_DAYS)
    return path


def log_click(screen, target, text, score, x, y, candidates):
    """点击时打醒目日志 + 存调试图"""
    global click_seq
    click_seq += 1
    sh, sw = screen.shape[:2]
    px, py = pos_pct(x, y, sw, sh)
    roi = TARGET_ROI.get(target, {"x": (0.0, 1.0), "y": (0.0, 1.0)})
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    now = datetime.now().strftime("%H:%M:%S")

    print()
    print("=" * 60)
    print(f"  >>> 点击 #{click_seq}  [{now}]  <<<")
    print("=" * 60)
    print(f"  目标关键字 : 「{target}」")
    print(f"  OCR 原文   : 「{text}」")
    print(f"  归一化文本 : 「{normalize_text(text)}」")
    print(f"  置信度     : {score:.3f}  (阈值>={OCR_SCORE})")
    print(f"  点击坐标   : ({x}, {y})   屏幕={sw}x{sh}")
    print(f"  相对位置   : 水平 {px:.1f}%  垂直 {py:.1f}%  (左上=0%, 右下=100%)")
    print(f"  允许 ROI   : x={roi['x']}  y={roi['y']}")
    print(f"  候选数量   : {len(candidates)}")
    for i, (sc, cx, cy, t) in enumerate(candidates[:8], 1):
        mark = " <-- 实际点击" if i == 1 else ""
        cpx, cpy = pos_pct(cx, cy, sw, sh)
        print(
            f"    [{i}] 「{t}」 score={sc:.3f} "
            f"@({int(cx)},{int(cy)}) ~({cpx:.1f}%,{cpy:.1f}%){mark}"
        )
    path = save_click_debug(screen, x, y, target, text, score, click_seq, ts)
    if path:
        print(f"  调试截图   : {path}")
    print("=" * 60)
    print()


def run_ocr(screen):
    """返回 [(text, score, cx, cy), ...]，坐标为原图像素"""
    h, w = screen.shape[:2]
    scale = 1.0
    img = screen
    if w > OCR_MAX_WIDTH:
        scale = OCR_MAX_WIDTH / float(w)
        img = cv2.resize(screen, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    result, _ = get_ocr()(img)
    items = []
    if not result:
        return items

    inv = 1.0 / scale
    for row in result:
        # row: [box, text, score]
        if not row or len(row) < 3:
            continue
        box, text, score = row[0], row[1], float(row[2])
        if score < OCR_SCORE:
            continue
        cx, cy = box_center(box)
        cx *= inv
        cy *= inv
        items.append((str(text), score, cx, cy))
    return items


def click_success_close(screen):
    """点击「领取成功」提示条右侧的 X，避免停在成功提示页。"""
    sh, sw = screen.shape[:2]
    x = int(sw * SUCCESS_CLOSE_X_RATIO)
    y = int(sh * SUCCESS_CLOSE_Y_RATIO)
    key = ("领取成功_X", x // 20, y // 20)
    now = time.time()
    if now - last_click.get(key, 0) < CLICK_DEDUPE_WINDOW:
        print("· 跳过重复点击「领取成功」X")
        return True
    last_click[key] = now
    print(f"· 点击「领取成功」右侧 X @({x},{y})")
    tap(x, y)
    return True


def log_hit(screen, hit, sw, sh):
    """统一记录一次命中（含层级信息）并保存带标记截图。"""
    global click_seq
    click_seq += 1
    x, y = hit.x, hit.y
    px, py = pos_pct(x, y, sw, sh)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    now = datetime.now().strftime("%H:%M:%S")

    print()
    print("=" * 60)
    print(f"  >>> 点击 #{click_seq}  [{now}]  <<<")
    print("=" * 60)
    print(f"  目标       : 「{hit.target}」")
    print(f"  命中层级   : {hit.level} / {hit.source}")
    print(f"  识别内容   : 「{hit.label}」")
    print(f"  置信度     : {hit.score:.3f}")
    print(f"  点击坐标   : ({x}, {y})   屏幕={sw}x{sh}")
    print(f"  相对位置   : 水平 {px:.1f}%  垂直 {py:.1f}%")
    print(f"  层级说明   : {hit.describe()}")
    path = save_click_debug(screen, x, y, hit.target, hit.label, hit.score, click_seq, ts)
    if path:
        print(f"  调试截图   : {path}")
    print("=" * 60)
    print()


def find_and_click(screen, items=None):
    """L0-L4 分层定位并点击。命中返回目标字符串，否则 None。"""
    sh, sw = screen.shape[:2]
    if items is None:
        items = run_ocr(screen)

    if LOG_OCR_EVERY_FRAME and items:
        preview = [
            f"{t}({s:.2f})@({int(x)},{int(y)})"
            for t, s, x, y in items
            if re.search(r"[\u4e00-\u9fff]", t)
        ]
        if preview:
            print("· 识别:", " | ".join(preview[:12]))

    # 必须识别到可操作按钮文字才点击；排除「XX秒后可领取奖励」这类倒计时提示
    actionable = [
        text
        for text, score, cx, cy in items
        if any(key in text for key in ACTION_KEYWORDS) and "秒后" not in text and "秒可" not in text
    ]
    if not actionable:
        return None

    rows = get_ui_rows() if ENABLE_LAYERED else []

    for target in TARGETS:
        if target == "领取成功":
            continue
        hit = locator.locate(
            target,
            rows=rows,
            screen=screen,
            ocr_items=items,
            sw=sw,
            sh=sh,
            dedupe_window=CLICK_DEDUPE_WINDOW,
        )
        if not hit:
            continue
        if LOG_HIT_LEVEL:
            print(f"· 命中层级: {hit.describe()}")
        log_hit(screen, hit, sw, sh)
        tap(hit.x, hit.y)
        return target

    return None


# 点完这些目标后不进入冷却，立刻下一轮识别
NO_COOLDOWN_TARGETS = {"领取成功"}

print("🚀 手机自动化工作台启动（uiautomator2 / OCR 文字识别）")
print(f"   目标: {TARGETS}  置信度>={OCR_SCORE}  轮询={POLL_INTERVAL}s")
print(f"   点击调试图目录: {DEBUG_DIR}")
print(f"   日志文件: {LOG_FILE if LOG_FILE else '未启用'}")
print(f"   无冷却目标: {sorted(NO_COOLDOWN_TARGETS)}")
connect_device()

def tap_text_if_visible(keywords, timeout=6):
    """在指定秒内轮询 OCR，识别到关键词就点它的实际坐标（自绘页面 UI 层级读不到文字）。"""
    import time as _t

    deadline = _t.time() + timeout
    while _t.time() < deadline:
        try:
            screen = screenshot()
            if screen is not None:
                items = run_ocr(screen)
                for kw in keywords:
                    for text, score, cx, cy in items:
                        if kw in text:
                            tap(int(cx), int(cy))
                            print(f"· 点击「{kw}」@({int(cx)},{int(cy)})")
                            _t.sleep(1.5)
                            return True
        except Exception as e:
            print(f"⚠️ 找入口文字失败: {e}")
        _t.sleep(0.4)
    print(f"· 未找到入口文字：{keywords}")
    return False


def navigate_to_entry():
    """按采集流程进入任务入口：福利 -> 领时长。"""
    if not AUTO_START_APP:
        return
    get_ocr()
    print("· 导航到任务入口：福利 -> 领时长")
    tap_text_if_visible(["福利"], timeout=8)
    tap_text_if_visible(["领时长"], timeout=8)


if AUTO_START_APP:
    print(f"· 自动启动 App: {APP_PACKAGE}")
    try:
        device.app_start(APP_PACKAGE)
        time.sleep(4)
    except Exception as e:
        print(f"⚠️ 自动启动失败: {e}")
    navigate_to_entry()


def ensure_target_app():
    """确保目标 App 在前台；否则拉回，避免在别的 App 里盲点。"""
    if not ONLY_TARGET_APP:
        return True
    try:
        current = device.app_current().get("package", "")
    except Exception:
        return True
    if current == APP_PACKAGE:
        return True
    print(f"· 当前在 {current or '未知页面'}，拉回 {APP_PACKAGE}")
    try:
        device.app_start(APP_PACKAGE)
    except Exception as e:
        print(f"⚠️ 拉回 App 失败: {e}")
    time.sleep(2)
    return False


miss_count = 0
while True:
    try:
        if not ensure_target_app():
            continue
        screen = screenshot()
        if screen is None:
            print("⚠️ 截图失败，检查 uiautomator2 连接")
            time.sleep(3)
            continue

        items = run_ocr(screen)
        record_ocr(items)
        sh_h = screen.shape[0]

        if is_countdown(items):
            if last_status != "countdown":
                print("· 广告倒计时中，等待目标出现")
                last_status = "countdown"
            miss_count = 0
            time.sleep(POLL_INTERVAL)
            continue
        if last_status == "countdown":
            print("· 倒计时结束，恢复识别")
            last_status = None

        if is_home_page(items):
            if AUTO_NAVIGATE_HOME and click_by_keyword(items, HOME_ENTRY_KEYWORDS):
                miss_count = 0
                time.sleep(2)
                continue
            if STOP_ON_HOME:
                print("✅ 已回到主页面，脚本停止（设置 STOP_ON_HOME=False 可继续）")
                break

        is_live = any(
            normalize_text(text) in ("关注", "更多直播") and cy < sh_h * 0.25
            for text, score, cx, cy in items
        )
        if is_live:
            print("⚡ 检测到顶部[关注/更多直播]，秒退直播间！")
            press_back()
            time.sleep(1.5)
            continue
        clicked = find_and_click(screen, items)
        if clicked:
            miss_count = 0
        if clicked:
            if clicked in NO_COOLDOWN_TARGETS:
                print(f"⚡ 「{clicked}」已处理，立即继续识别")
                continue
            time.sleep(CLICK_COOLDOWN)
            continue

        if ENABLE_RANDOM_SWIPE and random.random() < SWIPE_PROB:
            h, w = screen.shape[:2]
            x = w // 2
            y1 = int(h * 0.55)
            y2 = int(h * 0.40)
            print(f"↕ 随机上滑 ({x},{y1}) -> ({x},{y2})")
            device.swipe(x, y1, x, y2, 0.35)

        time.sleep(POLL_INTERVAL)
        miss_count += 1
        if miss_count == 1 or miss_count % 3 == 0:
            print(f"· 未找到目标（连续 {miss_count} 次）")
        if miss_count >= MISS_BACK_LIMIT:
            dump_ocr_snapshot(screen, "连续未匹配")
            print("⚠️ 连续未匹配，尝试返回上一页")
            press_back()
            miss_count = 0
            time.sleep(2)

    except KeyboardInterrupt:
        print("\n已停止")
        break
    except Exception as e:
        print(f"⚠️ 错误: {e}")
        time.sleep(5)
