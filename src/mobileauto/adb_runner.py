import os
import sys
import time
import random
import re
import shutil
import subprocess
import importlib
import importlib.util
import atexit
from datetime import datetime

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
LOG_FILE = os.path.join(RUNTIME_DIR, "mobileauto__adb.log")


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
AUTO_START_APP = False
APP_PACKAGE = "com.luna.music"
AUTO_NAVIGATE_HOME = False
HOME_ENTRY_KEYWORDS = ["福利中心", "免费听时长"]

# ===== 分层定位策略 L0-L4 =====
# L0 控件ID/稳定属性 → L1 控件文本 → L2 模板匹配 → L3 OCR → L4 比例坐标
ENABLE_LAYERED = True
LOG_HIT_LEVEL = True
# L0：resource-id 关键词映射，如 {"领取奖励": ["btn_reward", "claim"]}
TARGET_IDS = {}
# L0：content-desc 映射，默认自动把目标文字作为精确值
TARGET_DESCS = {}
# L2：模板匹配阈值（把按钮小图命名为「领取奖励.png」放到 templates/ 即生效）
TEMPLATE_THRESHOLD = 0.85
# L4：相对位置兜底，前面都失败才使用
TARGET_RATIO = {
    "领取奖励": (0.5, 0.52),
    "继续观看": (0.5, 0.52),
    "继续互动": (0.5, 0.52),
}
DEBUG_MAX_FILES = DEBUG_KEEP_MAX
DEBUG_MAX_DAYS = DEBUG_KEEP_DAYS
# ===========================================

DEBUG_DIR = DEBUG_CLICKS_DIR
OCR_DUMP_DIR = DEBUG_OCR_DIR


def find_adb():
    """按优先级查找 adb：ADB_PATH > 项目目录 > PATH > 常见 Android SDK 目录。"""
    adb_name = "adb.exe" if os.name == "nt" else "adb"
    candidates = []

    env_adb = os.environ.get("ADB_PATH")
    if env_adb:
        candidates.append(env_adb)

    candidates.append(os.path.join(SCRIPT_DIR, adb_name))

    which_adb = shutil.which("adb")
    if which_adb:
        candidates.append(which_adb)

    for env_key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk_root = os.environ.get(env_key)
        if sdk_root:
            candidates.append(os.path.join(sdk_root, "platform-tools", adb_name))

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "Android", "Sdk", "platform-tools", adb_name))

    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return None


ADB = find_adb()
locator = None
ocr_engine = None
click_seq = 0
last_click = {}
recent_ocr = []
last_status = None


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
    """通过 adb dump UI 层级供 L0/L1 使用；失败返回空，自动降级。"""
    try:
        remote = "/sdcard/window.xml"
        subprocess.run(
            [ADB, "shell", "uiautomator", "dump", remote],
            capture_output=True,
            text=True,
            timeout=30,
        )
        result = subprocess.run(
            [ADB, "shell", "cat", remote],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            return parse_hierarchy(result.stdout)
    except Exception:
        pass
    return []


init_locator()


def get_ocr():
    global ocr_engine
    if ocr_engine is None:
        print("⏳ 加载 OCR 模型...")
        ocr_engine = RapidOCR()
        print("✅ OCR 就绪")
    return ocr_engine


def adb_command(cmd):
    return subprocess.run([ADB] + cmd, capture_output=True, text=True)


def screenshot():
    result = subprocess.run([ADB, "exec-out", "screencap", "-p"], capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return None
    img = cv2.imdecode(np.frombuffer(result.stdout, np.uint8), cv2.IMREAD_COLOR)
    return img


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
    return any(re.search(r"\d+\s*秒", normalize_text(text)) for text, score, cx, cy in items)


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
    adb_command(["shell", "input", "tap", str(x), str(y)])
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
    adb_command(["shell", "input", "tap", str(x), str(y)])
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

    rows = get_ui_rows() if ENABLE_LAYERED else []

    for target in TARGETS:
        if target == "领取成功" and not CLICK_SUCCESS_BANNER:
            probe = locator.locate(
                target,
                rows=rows,
                screen=screen,
                ocr_items=items,
                sw=sw,
                sh=sh,
            )
            if not probe:
                continue
            close_hit = locator.locate_success_close(
                rows=rows, sw=sw, sh=sh, dedupe_window=CLICK_DEDUPE_WINDOW
            )
            if close_hit:
                if LOG_HIT_LEVEL:
                    print(f"· 命中层级: {close_hit.describe()}")
                adb_command(["shell", "input", "tap", str(close_hit.x), str(close_hit.y)])
            elif CLOSE_SUCCESS_BANNER:
                click_success_close(screen)
            else:
                print("· 检测到「领取成功」状态，未找到关闭控件")
            return target

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
        adb_command(["shell", "input", "tap", str(hit.x), str(hit.y)])
        return target

    return None


# 点完这些目标后不进入冷却，立刻下一轮识别
NO_COOLDOWN_TARGETS = {"领取成功"}

print("🚀 手机自动化工作台启动（OCR 文字识别）")
print(f"   目标: {TARGETS}  置信度>={OCR_SCORE}  轮询={POLL_INTERVAL}s")
print(f"   点击调试图目录: {DEBUG_DIR}")
print(f"   日志文件: {LOG_FILE if LOG_FILE else '未启用'}")
print(f"   无冷却目标: {sorted(NO_COOLDOWN_TARGETS)}")

if not ADB:
    print("❌ 找不到 adb.exe，脚本无法控制手机。")
    print("   解决：下载 Android platform-tools，把 adb.exe 放到本项目文件夹，")
    print("   或把 platform-tools 目录加入系统 PATH；也可设置环境变量 ADB_PATH 指向 adb.exe。")
    sys.exit(1)
print(f"   adb: {ADB}")

if AUTO_START_APP:
    print(f"· 自动启动 App: {APP_PACKAGE}")
    adb_command(["shell", "monkey", "-p", APP_PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"])
    time.sleep(3)

miss_count = 0
while True:
    try:
        screen = screenshot()
        if screen is None:
            print("⚠️ 截图失败，检查 adb 连接")
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
            adb_command(["shell", "input", "keyevent", "4"])
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
            adb_command(["shell", "input", "swipe", str(x), str(y1), str(x), str(y2), "350"])

        time.sleep(POLL_INTERVAL)
        miss_count += 1
        if miss_count == 1 or miss_count % 3 == 0:
            print(f"· 未找到目标（连续 {miss_count} 次）")
        if miss_count >= MISS_BACK_LIMIT:
            dump_ocr_snapshot(screen, "连续未匹配")
            print("⚠️ 连续未匹配，尝试返回上一页")
            adb_command(["shell", "input", "keyevent", "4"])
            miss_count = 0
            time.sleep(2)

    except KeyboardInterrupt:
        print("\n已停止")
        break
    except FileNotFoundError as e:
        print(f"❌ 找不到可执行文件: {e.filename}")
        print("   请确认 adb.exe 存在且路径正确（见启动时的 adb 路径）。")
        time.sleep(5)
    except Exception as e:
        print(f"⚠️ 错误: {e}")
        time.sleep(5)
